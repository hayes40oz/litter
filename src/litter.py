#!/usr/bin/env python

import socket
import os
import struct
import time
import sys
import json
import random
import threading
import urlparse
import Queue
import BaseHTTPServer
import logging
import urllib
import getopt
from litterstore import LitterStore, StoreError
from litterouter import *

# Log everything, and send it to stderr.
logging.basicConfig(level=logging.DEBUG)

class MulticastServer(threading.Thread):
    """Listens for multicast and put them in queue"""

    def __init__(self, queue, devs):
        threading.Thread.__init__(self)
        self.queue = queue
        self.running = threading.Event()
        self.intfs = [MulticastServer.get_ip(d) for d in devs]
        self.sock = MulticastServer.init_mcast(self.intfs)

    # from http://code.activestate.com/recipes/439094-get-the-ip-address-
    # associated-with-a-network-inter/
    @staticmethod
    def get_ip(ifname):
        """Retreives the ip address of an interface (Linux only)"""
        ip = ""
        if os.name != "nt":
            import fcntl
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            ip = socket.inet_ntoa(fcntl.ioctl(
                            s.fileno(),
                            0x8915,  # SIOCGIFADDR
                            struct.pack('256s', ifname[:15])
                            )[20:24])
        else:
            ip =([ip for ip in socket.gethostbyname_ex(socket.gethostname())[2]
                  if not ip.startswith("127.")][0]) 
        return ip

    @staticmethod
    def init_mcast(intfs=[], port=PORT, addr=MCAST_ADDR):
        """Initilizes a multicast socket"""

        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        if os.name != "nt":
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        #s.setsockopt(socket.SOL_IP, socket.IP_MULTICAST_TTL, 2)
        #s.setsockopt(socket.SOL_IP, socket.IP_MULTICAST_LOOP, 1)

        s.bind(('', port))

        for intf in intfs:
            s.setsockopt(socket.SOL_IP, socket.IP_ADD_MEMBERSHIP,
                socket.inet_aton(addr) + socket.inet_aton(intf))

        return s

    @staticmethod
    def close_mcast(s, addr=MCAST_ADDR):
        intf = s.getsockname()[0]
        s.setsockopt(socket.SOL_IP, socket.IP_DROP_MEMBERSHIP,
            socket.inet_aton(addr) + socket.inet_aton(intf))
        s.close()

    def run(self):
        """Waits in a loop for incoming packet then puts them in queue"""

        self.running.set() #set to true
        while self.running.is_set():
            data, addr = self.sock.recvfrom(1024)
            self.queue.put((data, UDPSender(self.sock, self.intfs, addr)))
            print "MulticastServer: sender ", repr(addr), data

    def stop(self):
        """Set run to false, and send an empty message"""

        self.running.clear() 
        msender = UDPSender(self.sock)
        msender.send("", (LOOP_ADDR, PORT))

    def __del__(self):
        self.close_mcast(self.sock)

class HTTPHandler(BaseHTTPServer.BaseHTTPRequestHandler):
    """Handles HTTP requests"""

    def do_GET(self):
        """Handles get requests"""

        try:
            if self.path.startswith('/api'):
                presults = urlparse.urlparse(self.path)
                request = urlparse.parse_qs(presults[4])
                self.process_request(request)
            else:
                self.process_file(self.path)
        except Exception as ex:
            self.send_error(400, str(ex))
            logging.exception(ex)

    def do_POST(self):
        """Handles post requests"""

        try:
            if self.path.startswith('/api'):
                presults = urlparse.urlparse(self.path)
                clen = int(self.headers.get('Content-Length'))
                request = urlparse.parse_qs(self.rfile.read(clen))
                self.process_request(request)
            else:
                self.process_file(self.path)
        except Exception as ex:
            self.send_error(400, str(ex))
            logging.exception(ex);

    def process_request(self, request):
        """Extract json from request and queues it for processing"""

        print "HTTPHandler: %s " % request
        data = request['json'][0]
        queue = Queue.Queue(1)
        sender = HTTPSender(queue, self.client_address)
        self.server.queue.put((data, sender), timeout=2)
        # waits for response from workerthread for only 2 seconds
        err, data = queue.get(timeout=2)

        if err:
            #Exception happened, TODO do something better here:
            self.send_error(500, str(err))
        else:
            self.send_response(200)
            self.send_header("Content-type", "text/x-json; charset=utf-8")
            self.end_headers()
            self.wfile.write(data.encode("utf-8"))

    def process_file(self, path):
        """Handles HTTP file requests"""

        if path == "/":
            self.send_file("web/litter.html", "text/html")
        elif path == "/litter.css":
            self.send_file("web/litter.css", "text/css")
        elif path == "/litter.js":
            self.send_file("web/litter.js", "text/javascript")
        elif path == "/jquery.js":
            self.send_file("web/jquery.js", "text/javascript")
        elif path == "/jquery-ui.js":
            self.send_file("web/jquery-ui.js", "text/javascript")
        elif path == "/jquery-ui.css":
            self.send_file("web/jquery-ui.css", "text/css")
        elif path == "/json2.js":
            self.send_file("web/json2.js", "text/javascript")
        elif path == "/md5.js":
            self.send_file("web/md5.js", "text/javascript")
        elif path == "/ping":
            #just to have a test method
            self.send_response(200)
            self.send_header("Content-type", "text/plain")
            self.end_headers()
            self.wfile.write("pong")
        else:
            self.send_error(404, "Not found")

    def send_file(self, path, ctype):
        try:
            f = open(path)
            data = f.read()
            f.close()
            self.send_response(200)
            self.send_header("Content-type", ctype)
            self.end_headers()
            self.wfile.write(data)
        except Exception as ex:
            logging.exception(ex)


class HTTPThread(threading.Thread):

    def __init__(self, queue, addr=LOOP_ADDR, port=8080):
        threading.Thread.__init__(self)
        self.port = port
        self.http = BaseHTTPServer.HTTPServer((IP_ANY, port), HTTPHandler)
        self.http.queue = queue
        self.running = threading.Event()

    def run(self):
        self.running.set()
        while self.running.is_set(): 
          self.http.handle_request()

    def stop(self):
        self.running.clear()
        #wake up the server:
        urllib.urlopen("http://127.0.0.1:%i/ping" % (self.port,)).read()


class WorkerThread(threading.Thread):

    def __init__(self, queue, name, sender):
        threading.Thread.__init__(self)
        self.queue = queue
        self.name = name
        self.sender = sender

    def run(self):
        # SQL database has to be created in same thread
        self.litstore = LitterStore(self.name)
        while True:
            data, sender = self.queue.get()
            if sender == None and data == None:
                # we close DB then break out of loop to stop thread
                self.litstore.close()
                break

            addr = None
            if sender != None: addr = sender.dest

            try:
                print "REQ: %s : %s" % (addr, data)
                data = unicode(data, "utf-8")
                request = json.loads(data)
                self.forward(request)

                # save method locally before sending it litterstore
                # just in case it gets modified
                response = self.litstore.process(request, repr(addr))
                print "REP: %s : %s" % (addr, response)
                self.send(response, sender)

            except StoreError as ie:
                if str(ie) == "column hashid is not unique":
                    #this means we got a duplicate, no need to log:
                    pass
                else:
                    if isinstance(sender, HTTPSender):
                        sender.send_error(ex)
                    logging.exception(ie)
            except Exception as ex:
                if isinstance(sender, HTTPSender):
                    sender.send_error(ex)
                logging.exception(ex)

    def forward(self, request):
        ttl = request.get('ttl', 1) - 1
        request['ttl'] = ttl
        if ttl > 0:
            data = json.dumps(request, ensure_ascii=False)
            self.sender.send(data.encode("utf-8"))

    def send(self, response, sender):

        if isinstance(sender, HTTPSender):
            # also send reply back to HTTP path directly from
            # thread, since it's put in queue for HTTP thread
            data = json.dumps(response, ensure_ascii=False)
            sender.send(data)

        if sender == None or isinstance(sender, HTTPSender):
            sender = self.sender

        for reply in response:
            data = json.dumps(reply, ensure_ascii=False)
            sender.send(data.encode("utf-8"))
            time.sleep(0.01)

    def stop(self):
        self.queue.put((None,None))


def usage():
    print "usage: ./litter.py [-i intf] [-n name] [-p port]"


def main():

    devs = ['lo']
    name = socket.gethostname()
    port = "8080";

    try:
        opts, args = getopt.getopt(sys.argv[1:], "i:n:p:")
    except getopt.GetoptError, err:
        usage()
        sys.exit()

    for o, a in opts:
        if o == "-i":
            devs.append(a)
        elif o == "-n":
            name = a
        elif o == "-p":
            port = a
        else:
            usage()
            sys.exit()

    queue = Queue.Queue(100)

    mserver = MulticastServer(queue, devs)
    mserver.start()

    wthread = WorkerThread(queue, name, UDPSender(mserver.sock, mserver.intfs))
    wthread.start()

    httpd = HTTPThread(queue, port=int(port))
    httpd.start()

    # wait a few seconds for threads to setup
    time.sleep(5)

    pull_req = { 'm' : 'pull_req'}
    pull_data = json.dumps(pull_req)

    gap_req = { 'm' : 'gap_req'}
    gap_data = json.dumps(gap_req)

    try:
        while True:
            queue.put((pull_data, None))
            queue.put((gap_data, None))
            time.sleep(60)
    except:
        #Control-C will put us here, let's stop the other threads:
        httpd.stop()
        mserver.stop()
        wthread.stop()

if __name__ == '__main__':
    main()

