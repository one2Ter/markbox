#!/usr/bin/env python
# Markbox - a blogging engine for Dropbox based on Markdown
# by Greg V <floatboth@me.com> http://floatboth.com

import os
import dropbox
import markdown
import cherrypy
from parsedatetime.parsedatetime import Calendar
from jinja2 import Environment, FileSystemLoader
from pyatom import AtomFeed
from time import mktime
from datetime import datetime

here = lambda a: os.path.join(os.path.dirname(__file__), a)

def ctype(ct):
    def decorator(fn):
        def wrapper(*args, **kwargs):
            cherrypy.response.headers['Content-Type'] = ct
            return fn(*args, **kwargs)
        return wrapper
    return decorator

def read_file(fname):
    try:
        with open(fname, "r") as f:
            return f.read()
    except IOError:
        return None

def get_markdown():
    return markdown.Markdown(extensions=["meta", "extra", "codehilite",
        "headerid(level=2)", "sane_lists", "smartypants"])

class Cache(object):
    "Wrapper around a memcache object with a decorator for caching handlers"

    def __init__(self): pass # inject backend and uncache_key later

    def cached(self, cachekey):
        def decorator(fn):
            def wrapper(*args, **kwargs):  # kwargs is query string
                ck = cachekey(args)
                if "uncache_key" in kwargs and kwargs["uncache_key"] == self.uncache_key:
                    self.backend.delete(ck)
                    content = None
                else:
                    content = self.backend.get(ck)
                if not content:
                    content = fn(*args, **kwargs)
                self.backend.set(ck, content)
                return content
            return wrapper
        return decorator

    def __getattr__(self, name):
        return getattr(self.backend, name)

class Markbox(object):
    cache = Cache()

    def __init__(self, public_folder="public", tpl_folder="templates",
            blog_title="Your New Markbox Blog", feed_name="articles",
            feed_author="Anonymous"):
        self.tpl = Environment(loader=FileSystemLoader([tpl_folder,
            here("templates")]))
        self.tpl.globals["blog_title"] = self.blog_title = blog_title
        self.tpl.globals["feed_name"] = self.feed_name = feed_name
        self.public_folder = public_folder
        self.feed_author = feed_author

        # CherryPy routes by method name, here we set the method name
        setattr(self, feed_name + "_xml", self._feed)

        if "MEMCACHE_SERVERS" in os.environ:
            import pylibmc
            self.cache.backend = pylibmc.Client(
                servers=[os.environ.get("MEMCACHE_SERVERS")],
                username=os.environ.get("MEMCACHE_USERNAME"),
                password=os.environ.get("MEMCACHE_PASSWORD"),
                binary=True)
        else:
            import mockcache
            self.cache.backend = mockcache.Client()

        if "DROPBOX_APP_KEY" in os.environ and \
                "DROPBOX_APP_SECRET" in os.environ:
            db_app_key = os.environ.get("DROPBOX_APP_KEY")
            db_app_secret = os.environ.get("DROPBOX_APP_SECRET")
        else:
            cherrypy.log("""Dropbox credentials not found in the env.
            Set DROPBOX_APP_KEY and DROPBOX_APP_SECRET env variables!""")
        self.dropbox = Dropbox(db_app_key, db_app_secret, self.cache)

        self.cache.uncache_key = os.environ.get("UNCACHE_KEY")
        if not self.cache.uncache_key:
            cherrypy.log("Uncache key not found in the env.")

        def handle_404(status, message, traceback, version):
            tpl_404 = self.tpl.get_template("404.html")
            return tpl_404.render(page_title="Page not found")

        cherrypy.config.update({"error_page.404": handle_404})

    @cherrypy.expose
    @ctype("application/atom+xml; charset=utf-8")
    @cache.cached(lambda a: "feed")
    def _feed(self, *args, **kwargs):
        self.dropbox.connect(kwargs)
        try:
            host = cherrypy.request.base
            atom = AtomFeed(title=self.blog_title, url=host,
                    feed_url=cherrypy.url(),
                    author=self.feed_author)
            for post in self.dropbox.listing():
                atom.add(title=post["title"],
                        url=host+post["path"],
                        author=self.feed_author,
                        content_type="html",
                        content=post["html"],
                        updated=post["date"])
            return atom.to_string()
        except dropbox.rest.ErrorResponse, e:
            return self.dropbox_error(e)

    @cherrypy.expose
    @cache.cached(lambda a: a[1])  # title from (self, title)
    def default(self, title, **kwargs):
        self.dropbox.connect(kwargs)
        try:
            src = self.dropbox.read_file(title + ".md")
            mdown = get_markdown()
            html = mdown.convert(src)
            tpl_post = self.tpl.get_template("post.html")
            return tpl_post.render(body=html,
                    page_title=mdown.Meta["title"][0],
                    date=mdown.Meta["date"][0])
        except dropbox.rest.ErrorResponse, e:
            if e.status == 404:
                raise cherrypy.HTTPError(404, "File not found")
            else:
                return self.dropbox_error(e)

    @cherrypy.expose
    @cache.cached(lambda a: "index")
    def index(self, *args, **kwargs):
        self.dropbox.connect(kwargs)
        try:
            tpl_list = self.tpl.get_template("list.html")
            return tpl_list.render(posts=self.dropbox.listing())
        except dropbox.rest.ErrorResponse, e:
            return self.dropbox_error(e)

    def run(self, host="0.0.0.0", port=8080):
        cherrypy.config.update({
            "server.socket_host": host,
            "server.socket_port": port
        })
        cherrypy.quickstart(self, "/", {
            "/"+os.path.basename(self.public_folder): {
                "tools.staticdir.on": True,
                "tools.staticdir.dir": self.public_folder
            }
        })

    def dropbox_error(self, e):
        import traceback
        return "<!DOCTYPE html><pre>Dropbox error: " + \
                traceback.format_exc(e) + "</pre>"

class Dropbox(object):
    cal = Calendar()

    def __init__(self, app_key, app_secret, cache):
        self.app_key = app_key
        self.app_secret = app_secret
        self.cache = cache
        self.client = None

    def listing(self):
        files = self.client.search("/", ".md")
        posts = []
        for f in files:
            cont = self.read_file(f["path"])
            mdown = get_markdown()
            html = mdown.convert(cont)
            if "title" in mdown.Meta and "date" in mdown.Meta:
                posts.append({
                    "path": f["path"][:-3],  # no extension, keep slash
                    "title": mdown.Meta["title"][0],  # wrapped in a list
                    "date": datetime.fromtimestamp(mktime(self.cal.parse(mdown.Meta["date"][0])[0])),
                    "html": html
                })
            else:
                cherrypy.log("No title and/or date in file: " + f["path"])
        posts = sorted(posts, key=lambda p: p["date"])
        posts.reverse()
        return posts

    def read_file(self, fname):
        r = self.client.get_file(fname)
        cont = r.read()
        r.close()
        return cont

    def connect(self, query):
        if not self.client:
            sess = dropbox.session.DropboxSession(self.app_key,
                    self.app_secret, "app_folder")
            # Access token is saved to memcache and the filesystem
            s_token = self.cache.get("s_token") or read_file(".s_token")
            s_token_secret = self.cache.get("s_token_secret") or read_file(".s_token_secret")
            if s_token and s_token_secret:
                sess.set_token(s_token, s_token_secret)
            elif "oauth_token" in query:  # callback from Dropbox
                s_token = sess.obtain_access_token(dropbox.session.OAuthToken(\
                    self.cache.get("r_token"), self.cache.get("r_token_secret")))
                self.cache.set("s_token", s_token.key)
                self.cache.set("s_token_secret", s_token.secret)
                with open(".s_token", "w") as f:
                    f.write(s_token.key)
                with open(".s_token_secret", "w") as f:
                    f.write(s_token.secret)
                self.cache.delete("r_token")
                self.cache.delete("r_token_secret")
            else:  # start of Dropbox auth
                req_token = sess.obtain_request_token()
                self.cache.set("r_token", req_token.key)
                self.cache.set("r_token_secret", req_token.secret)
                url = sess.build_authorize_url(req_token, cherrypy.url())
                raise cherrypy.HTTPRedirect(url)
            self.client = dropbox.client.DropboxClient(sess)
