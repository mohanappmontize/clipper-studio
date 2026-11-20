#!/usr/bin/env python3
"""Check that /media only serves files inside the clipper work folder.

Starts the real Handler on a temporary port, points WORK at a temporary folder and asks for files
inside it, files outside it, ../ escapes, a symlinked directory and the work folder itself.

  python3 ui/check_media.py          # exit 0 = every case behaved
"""
import http.client, os, shutil, sys, tempfile, threading, urllib.parse
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server


def main():
    tmp = tempfile.mkdtemp(prefix="clipper_media_")
    try:
        work = os.path.join(tmp, "work")
        os.makedirs(work)
        inside = os.path.join(work, "inside.txt")
        open(inside, "w").write("inside the work folder")
        outside = os.path.join(tmp, "outside.txt")
        open(outside, "w").write("secret")
        secrets = os.path.join(tmp, "secrets")
        os.makedirs(secrets)
        open(os.path.join(secrets, "password.txt"), "w").write("secret")
        link_dir = os.path.join(work, "evil")
        os.symlink(secrets, link_dir)                    # a directory link out of the work folder
        link_file = os.path.join(work, "source.mp4")
        os.symlink(outside, link_file)                   # ingest stores sources exactly like this

        server.WORK = work
        srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]

        def get(path):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            c.request("GET", "/media?path=" + urllib.parse.quote(path))
            r = c.getresponse(); body = r.read(); c.close()
            return r.status, body

        cases = [
            ("a file inside the work folder", inside, 200),
            ("a missing file inside the work folder", os.path.join(work, "nope.mp4"), 404),
            ("an absolute path outside the work folder", outside, 403),
            ("a ../ escape from the work folder", os.path.join(work, "..", "outside.txt"), 403),
            ("a symlinked directory inside pointing outside", os.path.join(link_dir, "password.txt"), 403),
            ("the work folder itself", work, 403),
            ("a source symlink (how ingest stores a video)", link_file, 200),
        ]
        fails = 0
        for label, path, want in cases:
            got, body = get(path)
            ok = got == want
            if ok and want == 200:
                ok = body == open(os.path.realpath(path), "rb").read()
            fails += not ok
            print(("PASS  " if ok else "FAIL  ") + "%s -> %d (want %d)" % (label, got, want))
        srv.shutdown()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("media scope: %s" % ("ok" if not fails else "%d case(s) failed" % fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
