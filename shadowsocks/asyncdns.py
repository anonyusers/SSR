#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright 2014-2015 clowwindy
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

from __future__ import absolute_import, division, print_function, with_statement

import os
import socket
import struct
import re
import logging

if __name__ == '__main__':
    import sys
    import inspect
    file_path = os.path.dirname(os.path.realpath(inspect.getfile(inspect.currentframe())))
    sys.path.insert(0, os.path.join(file_path, '../'))

from shadowsocks import common, lru_cache, eventloop, shell


CACHE_SWEEP_INTERVAL = 30

# 匹配有效的域名格式
VALID_HOSTNAME = re.compile(br"(?!-)[A-Z\d_-]{1,63}(?<!-)$", re.IGNORECASE)

common.patch_socket()

# rfc1035  DNS请求报文格式
# format
# +---------------------+
# |        Header       | 定义报文是请求还是应答
# +---------------------+
# |       Question      | the question for the name server,类型(QTYPE),类(QCLASS)或域名(QNAME)
# +---------------------+
# |        Answer       | RRs answering the question
# +---------------------+
# |      Authority      | RRs pointing toward an authority
# +---------------------+
# |      Additional     | RRs holding additional information
# +---------------------+
#
# header 请求报文头
#                                 1  1  1  1  1  1
#   0  1  2  3  4  5  6  7  8  9  0  1  2  3  4  5
# +--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+
# |                      ID                       | 16bit
# +--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+
# |QR|   Opcode  |AA|TC|RD|RA|   Z    |   RCODE   | 1+4+1+1+1+1+3+4 = 16
# +--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+
# |                    QDCOUNT                    |
# +--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+
# |                    ANCOUNT                    |
# +--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+
# |                    NSCOUNT                    |
# +--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+
# |                    ARCOUNT                    |
# +--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+

# 请求类型
QTYPE_ANY = 255
QTYPE_A = 1 # 请求一条A记录,正向域名解析,解析域名为一条ipv4地址记录
QTYPE_AAAA = 28 # 解析域名为一条ipv6地址记录
QTYPE_CNAME = 5 # 将域名指向域名
QTYPE_NS = 2 # 域名解析服务器记录
QCLASS_IN = 1 # 查询因特网协议类型

def detect_ipv6_supprot():
    if 'has_ipv6' in dir(socket):
        try:
            s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) # 创建一个ipv6协议族socket
            s.connect(('::1', 0)) # ::1表示本地ipv6地址,类似127.0.0.1
            print('IPv6 support')
            return True
        except:
            pass
    print('IPv6 not support')
    return False

IPV6_CONNECTION_SUPPORT = detect_ipv6_supprot()

def build_address(address): 
    # 处理二进制字符串
    address = address.strip(b'.') # 去除两边多余的.
    labels = address.split(b'.') # 按.切分为列表
    results = []
    for label in labels:
        l = len(label)
        if l > 63:
            return None
        results.append(common.chr(l)) # 补充元素大小
        results.append(label) # 追加元素
    results.append(b'\0')
    return b''.join(results)

def build_request(address, qtype):
    # 构造请求id,16 bit
    request_id = os.urandom(2) # 产生2字节大小的bytes类型的随机数
    # 构造请求头,16x5 bit
    header = struct.pack('!BBHHHH', 1, 0, 1, 0, 0, 0) # 按格式组成字节流
    addr = build_address(address) # 构造二进制地址
    # 构造请求类型(16bit)和查询类型(16bit),
    qtype_qclass = struct.pack('!HH', qtype, QCLASS_IN)
    return request_id + header + addr + qtype_qclass

def parse_ip(addrtype, data, length, offset):
    # 解析地址,反馈查询
    if addrtype == QTYPE_A:
        return socket.inet_ntop(socket.AF_INET, data[offset:offset + length])
    elif addrtype == QTYPE_AAAA:
        return socket.inet_ntop(socket.AF_INET6, data[offset:offset + length])
    elif addrtype in [QTYPE_CNAME, QTYPE_NS]:
        return parse_name(data, offset)[1]
    else:
        return data[offset:offset + length]


def parse_name(data, offset):
    p = offset
    labels = []
    l = common.ord(data[p])
    while l > 0:
        if (l & (128 + 64)) == (128 + 64):
            # pointer
            pointer = struct.unpack('!H', data[p:p + 2])[0]
            pointer &= 0x3FFF
            r = parse_name(data, pointer)
            labels.append(r[1])
            p += 2
            # pointer is the end
            return p - offset, b'.'.join(labels)
        else:
            labels.append(data[p + 1:p + 1 + l])
            p += 1 + l
        l = common.ord(data[p])
    return p - offset + 1, b'.'.join(labels)


# rfc1035 DNS应答报文格式
# record 资源记录格式
#                                    1  1  1  1  1  1
#      0  1  2  3  4  5  6  7  8  9  0  1  2  3  4  5
#    +--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+
#    |                                               |
#    /                                               /
#    /                      NAME                     /  8 bytes
#    |                                               |
#    +--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+
#    |                      TYPE                     |  2 bytes
#    +--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+
#    |                     CLASS                     |  2 bytes
#    +--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+
#    |                      TTL                      |  4 bytes
#    |                                               |
#    +--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+
#    |                   RDLENGTH                    |  2 bytes
#    +--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+--|
#    /                     RDATA                     /  4 bytes
#    /                                               /
#    +--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+
def parse_record(data, offset, question=False):
    nlen, name = parse_name(data, offset)
    if not question:
        record_type, record_class, record_ttl, record_rdlength = struct.unpack(
            '!HHiH', data[offset + nlen:offset + nlen + 10]
        )
        ip = parse_ip(record_type, data, record_rdlength, offset + nlen + 10)
        return nlen + 10 + record_rdlength, \
            (name, ip, record_type, record_class, record_ttl)
    else:
        record_type, record_class = struct.unpack(
            '!HH', data[offset + nlen:offset + nlen + 4]
        )
        return nlen + 4, (name, None, record_type, record_class, None, None)


def parse_header(data):
    if len(data) >= 12:
        header = struct.unpack('!HBBHHHH', data[:12])
        res_id = header[0]
        res_qr = header[1] & 128
        res_tc = header[1] & 2
        res_ra = header[2] & 128
        res_rcode = header[2] & 15
        # assert res_tc == 0
        # assert res_rcode in [0, 3]
        res_qdcount = header[3]
        res_ancount = header[4]
        res_nscount = header[5]
        res_arcount = header[6]
        return (res_id, res_qr, res_tc, res_ra, res_rcode, res_qdcount,
                res_ancount, res_nscount, res_arcount)
    return None


def parse_response(data):
    try:
        if len(data) >= 12:
            header = parse_header(data)
            if not header:
                return None
            res_id, res_qr, res_tc, res_ra, res_rcode, res_qdcount, \
                res_ancount, res_nscount, res_arcount = header

            qds = []
            ans = []
            offset = 12
            for i in range(0, res_qdcount):
                l, r = parse_record(data, offset, True)
                offset += l
                if r:
                    qds.append(r)
            for i in range(0, res_ancount):
                l, r = parse_record(data, offset)
                offset += l
                if r:
                    ans.append(r)
            for i in range(0, res_nscount):
                l, r = parse_record(data, offset)
                offset += l
            for i in range(0, res_arcount):
                l, r = parse_record(data, offset)
                offset += l
            response = DNSResponse()
            if qds:
                response.hostname = qds[0][0]
            for an in qds:
                response.questions.append((an[1], an[2], an[3]))
            for an in ans:
                response.answers.append((an[1], an[2], an[3]))
            return response
    except Exception as e:
        shell.print_exception(e)
        return None


def is_valid_hostname(hostname):
    # 检测域名格式是否有效
    if len(hostname) > 255:
        return False
    if hostname[-1] == b'.':
        hostname = hostname[:-1]
    return all(VALID_HOSTNAME.match(x) for x in hostname.split(b'.'))


class DNSResponse(object): # DNS响应类
    def __init__(self):
        self.hostname = None
        self.questions = []  # each: (addr, type, class)
        self.answers = []  # each: (addr, type, class)

    def __str__(self):
        return '%s: %s' % (self.hostname, str(self.answers))


STATUS_IPV4 = 0
STATUS_IPV6 = 1


class DNSResolver(object): # DNS解析类

    def __init__(self):
        self._loop = None
        self._hosts = {}
        self._hostname_status = {}
        self._hostname_to_cb = {}
        self._cb_to_hostname = {}
        self._cache = lru_cache.LRUCache(timeout=300)
        self._sock = None
        self._servers = None
        self._parse_resolv()
        self._parse_hosts()
        # TODO monitor hosts change and reload hosts
        # TODO parse /etc/gai.conf and follow its rules

    def _parse_resolv(self):
        self._servers = []
        try:
            with open('dns.conf', 'rb') as f: #用户层dns域名服务器定义
                content = f.readlines()
                for line in content:
                    line = line.strip()
                    if line:
                        parts = line.split(b' ', 1)
                        if len(parts) >= 2:
                            server = parts[0]
                            port = int(parts[1])
                        else:
                            server = parts[0]
                            port = 53
                        if common.is_ip(server) == socket.AF_INET:
                            if type(server) != str:
                                server = server.decode('utf8')
                            self._servers.append((server, port))
        except IOError:
            pass
        if not self._servers: # 系统层dns域名服务器定义
            try: 
                with open('/etc/resolv.conf', 'rb') as f:
                    content = f.readlines()
                    for line in content:
                        line = line.strip()
                        if line:
                            if line.startswith(b'nameserver'):
                                parts = line.split()
                                if len(parts) >= 2:
                                    server = parts[1]
                                    if common.is_ip(server) == socket.AF_INET:
                                        if type(server) != str:
                                            server = server.decode('utf8')
                                        self._servers.append((server, 53))
            except IOError:
                pass
        if not self._servers: # 设置默认的dns域名服务器
            self._servers = [('8.8.4.4', 53), ('8.8.8.8', 53), ('114.114.114.114', 53)]
        logging.info('dns server: %s' % (self._servers,))

    def _parse_hosts(self):
        etc_path = '/etc/hosts'
        if 'WINDIR' in os.environ:
            # Windows系统下的hosts文件位置
            etc_path = os.environ['WINDIR'] + '/system32/drivers/etc/hosts'
        try:
            with open(etc_path, 'rb') as f:
                for line in f.readlines():
                    line = line.strip()
                    if b"#" in line:
                        line = line[:line.find(b'#')]
                    parts = line.split()
                    if len(parts) >= 2:
                        ip = parts[0]
                        if common.is_ip(ip):
                            for i in range(1, len(parts)):
                                hostname = parts[i]
                                if hostname:
                                    self._hosts[hostname] = ip
        except IOError:
            self._hosts['localhost'] = '127.0.0.1'

    def add_to_loop(self, loop):
        if self._loop:
            raise Exception('already add to loop')
        self._loop = loop
        # TODO when dns server is IPv6
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM,
                                   socket.SOL_UDP)
        self._sock.setblocking(False)
        loop.add(self._sock, eventloop.POLL_IN, self)
        loop.add_periodic(self.handle_periodic)

    def _call_callback(self, hostname, ip, error=None):
        callbacks = self._hostname_to_cb.get(hostname, [])
        for callback in callbacks:
            if callback in self._cb_to_hostname:
                del self._cb_to_hostname[callback]
            if ip or error:
                callback((hostname, ip), error)
            else:
                callback((hostname, None),
                         Exception('unable to parse hostname %s' % hostname))
        if hostname in self._hostname_to_cb:
            del self._hostname_to_cb[hostname]
        if hostname in self._hostname_status:
            del self._hostname_status[hostname]

    def _handle_data(self, data):
        response = parse_response(data)
        if response and response.hostname:
            hostname = response.hostname
            ip = None
            for answer in response.answers:
                if answer[1] in (QTYPE_A, QTYPE_AAAA) and \
                        answer[2] == QCLASS_IN:
                    ip = answer[0]
                    break
            if IPV6_CONNECTION_SUPPORT:
                if not ip and self._hostname_status.get(hostname, STATUS_IPV4) \
                        == STATUS_IPV6:
                    self._hostname_status[hostname] = STATUS_IPV4
                    self._send_req(hostname, QTYPE_A)
                else:
                    if ip:
                        self._cache[hostname] = ip
                        self._call_callback(hostname, ip)
                    elif self._hostname_status.get(hostname, None) == STATUS_IPV4:
                        for question in response.questions:
                            if question[1] == QTYPE_A:
                                self._call_callback(hostname, None)
                                break
            else:
                if not ip and self._hostname_status.get(hostname, STATUS_IPV6) \
                        == STATUS_IPV4:
                    self._hostname_status[hostname] = STATUS_IPV6
                    self._send_req(hostname, QTYPE_AAAA)
                else:
                    if ip:
                        self._cache[hostname] = ip
                        self._call_callback(hostname, ip)
                    elif self._hostname_status.get(hostname, None) == STATUS_IPV6:
                        for question in response.questions:
                            if question[1] == QTYPE_AAAA:
                                self._call_callback(hostname, None)
                                break

    def handle_event(self, sock, fd, event):
        if sock != self._sock:
            return
        if event & eventloop.POLL_ERR:
            logging.error('dns socket err')
            self._loop.remove(self._sock)
            self._sock.close()
            # TODO when dns server is IPv6
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM,
                                       socket.SOL_UDP)
            self._sock.setblocking(False)
            self._loop.add(self._sock, eventloop.POLL_IN, self)
        else:
            data, addr = sock.recvfrom(1024)
            if addr not in self._servers:
                logging.warn('received a packet other than our dns')
                return
            self._handle_data(data)

    def handle_periodic(self):
        self._cache.sweep()

    def remove_callback(self, callback):
        hostname = self._cb_to_hostname.get(callback)
        if hostname:
            del self._cb_to_hostname[callback]
            arr = self._hostname_to_cb.get(hostname, None)
            if arr:
                arr.remove(callback)
                if not arr:
                    del self._hostname_to_cb[hostname]
                    if hostname in self._hostname_status:
                        del self._hostname_status[hostname]

    def _send_req(self, hostname, qtype):
        req = build_request(hostname, qtype)
        for server in self._servers:
            logging.debug('resolving %s with type %d using server %s',
                          hostname, qtype, server)
            self._sock.sendto(req, server)

    def resolve(self, hostname, callback):
        if type(hostname) != bytes:
            hostname = hostname.encode('utf8')
        if not hostname:
            callback(None, Exception('empty hostname'))
        elif common.is_ip(hostname):
            callback((hostname, hostname), None)
        elif hostname in self._hosts:
            logging.debug('hit hosts: %s', hostname)
            ip = self._hosts[hostname]
            callback((hostname, ip), None)
        elif hostname in self._cache:
            logging.debug('hit cache: %s', hostname)
            ip = self._cache[hostname]
            callback((hostname, ip), None)
        else:
            if not is_valid_hostname(hostname):
                callback(None, Exception('invalid hostname: %s' % hostname))
                return
            if False:
                addrs = socket.getaddrinfo(hostname, 0, 0,
                                           socket.SOCK_DGRAM, 
                                           socket.SOL_UDP)
                if addrs:
                    af, socktype, proto, canonname, sa = addrs[0]
                    logging.debug('DNS resolve %s %s' % (hostname, sa[0]) )
                    self._cache[hostname] = sa[0]
                    callback((hostname, sa[0]), None)
                    return
            arr = self._hostname_to_cb.get(hostname, None)
            if not arr:
                if IPV6_CONNECTION_SUPPORT:
                    self._hostname_status[hostname] = STATUS_IPV6
                    self._send_req(hostname, QTYPE_AAAA)
                else:
                    self._hostname_status[hostname] = STATUS_IPV4
                    self._send_req(hostname, QTYPE_A)
                self._hostname_to_cb[hostname] = [callback]
                self._cb_to_hostname[callback] = hostname
            else:
                arr.append(callback)
                # TODO send again only if waited too long
                if IPV6_CONNECTION_SUPPORT:
                    self._send_req(hostname, QTYPE_AAAA)
                else:
                    self._send_req(hostname, QTYPE_A)

    def close(self):
        if self._sock:
            if self._loop:
                self._loop.remove_periodic(self.handle_periodic)
                self._loop.remove(self._sock)
            self._sock.close()
            self._sock = None


def test():
    dns_resolver = DNSResolver()
    loop = eventloop.EventLoop()
    dns_resolver.add_to_loop(loop)

    global counter
    counter = 0

    def make_callback():
        global counter

        def callback(result, error):
            global counter
            # TODO: what can we assert?
            print(result, error)
            counter += 1
            if counter == 9:
                dns_resolver.close()
                loop.stop()
        a_callback = callback
        return a_callback

    assert(make_callback() != make_callback())

    dns_resolver.resolve(b'google.com', make_callback())
    dns_resolver.resolve('google.com', make_callback())
    dns_resolver.resolve('example.com', make_callback())
    dns_resolver.resolve('ipv6.google.com', make_callback())
    dns_resolver.resolve('www.facebook.com', make_callback())
    dns_resolver.resolve('ns2.google.com', make_callback())
    dns_resolver.resolve('invalid.@!#$%^&$@.hostname', make_callback())
    dns_resolver.resolve('toooooooooooooooooooooooooooooooooooooooooooooooooo'
                         'ooooooooooooooooooooooooooooooooooooooooooooooooooo'
                         'long.hostname', make_callback())
    dns_resolver.resolve('toooooooooooooooooooooooooooooooooooooooooooooooooo'
                         'ooooooooooooooooooooooooooooooooooooooooooooooooooo'
                         'ooooooooooooooooooooooooooooooooooooooooooooooooooo'
                         'ooooooooooooooooooooooooooooooooooooooooooooooooooo'
                         'ooooooooooooooooooooooooooooooooooooooooooooooooooo'
                         'ooooooooooooooooooooooooooooooooooooooooooooooooooo'
                         'long.hostname', make_callback())

    loop.run()


if __name__ == '__main__':
    test()
