import socket
import ssl

def test():
    s = socket.socket()
    s.connect(("localhost",143))
    s.settimeout(10)
    ret = s.recv(1024)
    print ret
    if "STARTTLS" in ret:
        print "Starting TLS"
        s.send("1 STARTTLS\r\n")
        ret = s.recv(1024)
        if not ret.startswith("1 OK"):
            print "Error starting TLS"
            print ret
            return False
        ss = ssl.wrap_socket(s)
        ss.write("a CAPABILITY\r\n")
        print ss.recv(1024)
    

if __name__ == "__main__":
    test()
