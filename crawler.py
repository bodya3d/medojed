from bottle import Bottle, view, request, redirect
from wtforms import Form, StringField, IntegerField, BooleanField, validators

import urllib.request

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from model import Base, Page, Relation
from sqlalchemy.engine.url import URL
import sqlalchemy as sa

import config

import urllib.request
import urllib.parse
import urllib.robotparser
from bs4 import BeautifulSoup
from queue import Queue
import threading
import time
import gzip

crawler_app = Bottle()

engine = create_engine(URL(**config.DATABASE))
# Base.metadata.bind = engine
sa.orm.configure_mappers()
Base.metadata.create_all(engine)

DBSession = sessionmaker(bind=engine)
session = DBSession()


class CrawlerFormProcessor(Form):
    url = StringField('URL', [validators.URL(require_tld=False, message="Must be valid URL")],
                      render_kw={"placeholder": "https://example.com"})
    depth = IntegerField('Depth', [validators.NumberRange(min=1, message="Must be > 0")], default=1)
    max_pages = IntegerField('Maximum pages', [validators.NumberRange(min=1, message="Must be > 0")], default=1000)
    uel = BooleanField('Include external links')

db_lock = threading.Lock()

def add_page_with_text_to_database(page, text):
    with db_lock:
        global cm
        cm+=1
        new_page = Page(url=page, text=text, rank=0)
        try:
            q = session.query(Page).filter(Page.url == page)
            if q.count() == 0:
                print('Added to "page" db: ' + page)
                session.add(new_page)
                session.commit()
            else:
                obj = q.first()
                obj.text = text
                session.commit()
        except:
            global cmexcept
            cmexcept += 1
            session.rollback()
            raise
        finally:
            session.close()


def add_page_pair_to_database(from_page, to_page):
    with db_lock:
        cou = session.query(Page).filter(Page.url == from_page).count()
        cou1 = session.query(Page).filter(Page.url == to_page).count()
        if cou == 0:
            new_page = Page(url=from_page, text="", rank=0)
            session.add(new_page)
            session.commit()
        if cou1 == 0:
            new_page = Page(url=to_page, text="", rank=0)
            session.add(new_page)
            session.commit()
        i = session.query(Page).filter(Page.url == from_page).first()
        i1 = session.query(Page).filter(Page.url == to_page).first()

        new_relation = Relation(page_id = i.id, destination_id = i1.id)
        # print(new_relation.page_id.id)
        session.add(new_relation)
        session.commit()

        # print('Added to "relation" db: ', i.id, i1.id)


class Crawler:

    def __init__(self, website, depth=3, pages_limit=0, threads_number=16, remove_external_links=True):
        # settings
        self.website = self.make_requestable_link(website)
        self.depth = depth
        self.pages_limit = pages_limit
        self.threads_number = threads_number
        self.remove_external_links = remove_external_links
        self.base = self.make_base(self.website)
        print("Crawler initialized!")
        print("Website = ", self.website)
        print("Base = ", self.base)

        # threading
        self.q = Queue()
        self.processed_lock = threading.Lock()
        self.pages_counter_lock = threading.Lock()

        # processing
        self.processed = set()
        self.robot_parser = urllib.robotparser.RobotFileParser()
        self.current_pages_processed = 1

        # output
        self.dictionary = {}

    @classmethod
    def make_requestable_link(cls, website):
        # add 'http' to the link if needed
        if website.find("http://") != 0 and website.find("https://") != 0:
            website = "http://" + website
        return website

    @classmethod
    def make_base(cls, website):
        # domain base
        temp_base = website[7:]
        slash_pos = temp_base.find('/')
        if slash_pos != -1:
            temp_base = temp_base[:slash_pos]
        temp_base = ".".join(temp_base.split(".")[-2:])
        # print("Base =", temp_base)
        return temp_base

    def get_outlinks(self, wb):

        # init resulting set
        results = set()

        # print('Website link :', wb)
        request = urllib.request.Request(
            wb,
            headers={
                "Accept-Encoding": "gzip"
            })

        # get header and content
        gzip_ = False
        try:
            with urllib.request.urlopen(request, timeout=15) as url:
                info = url.info()
                # print(info["Content-Encoding"])
                if info["Content-Encoding"] == "gzip":
                    gzip_ = True
        except IOError as e:
            print("Couldn't get info for url", wb, e)
            return set()

        # discard non-html
        if info is None:
            return set()
        if info['Content-Type'].find("html") == -1:
            print("Error : It's not an html page!", wb)
            return set()

        # get header and content
        try:
            with urllib.request.urlopen(request, timeout=15) as url:
                if not gzip_:
                    page = url.read()
                else:
                    page = gzip.decompress(url.read())
                    # print("Decompressed")
        except IOError:
            print("Couldn't open url", wb)
            return set()

        # prepare soup
        soup = BeautifulSoup(page, "html.parser")

        # http://stackoverflow.com/a/24618186
        for script in soup(["script", "style"]):
            script.extract()  # rip it out

        text = soup.get_text()

        # break into lines and remove leading and trailing space on each
        lines = (line.strip() for line in text.splitlines())
        # break multi-headlines into a line each
        chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
        # drop blank lines
        text = '\n'.join(chunk for chunk in chunks if chunk)

        add_page_with_text_to_database(wb, text)

        # prepare soup
        # soup = BeautifulSoup(page, "html.parser")

        for link in soup.find_all('a'):
            temp = link.get('href')

            # print("$",temp,"$")

            # skip empty
            if temp is None:
                continue
            if len(temp) == 0:
                continue
            if temp.isspace():
                continue
            if temp == "?":
                continue

            # fix relative links
            temp = urllib.parse.urljoin(wb, temp)
            # print("Fixed relative", temp)

            # throw away anchors
            if temp[0] == '#':
                continue

            # cut anchors from urls at the end
            if temp.rfind('#') != -1:
                temp = temp[:temp.rfind('#')]

            # throwaway javascript: , mailto: and anything like them
            if temp[:4] != "http":
                continue

            if self.remove_external_links:
                base_pos = temp.find(self.base)
                sl = temp[8:].find("/") + 8
                # print("For", temp, "base_pos =", base_pos, "sl =", sl)
                if base_pos == -1 or (sl != -1 and sl < base_pos):
                    continue
            # print("Adding", temp)
            results.add(temp)
        return results

    def worker(self):
        debug = True

        while True:
            # get task from queue
            current = self.q.get()

            # are we done yet?
            if current is None:
                break

            current_depth = current[0]
            current_url = current[1]
            new_depth = current_depth + 1

            # check if it has not been taken
            with self.processed_lock:
                if debug:
                    print(threading.current_thread().name, "requests", current_depth, current_url)
                self.processed.add(current_url)

            # should we go below that depth?
            # if current_depth > self.depth:
            #     print("Break because of depth")
            #     break

            # do the work
            res = self.get_outlinks(current_url)

            # add new links to the queue
            if new_depth <= self.depth:
                with self.processed_lock:
                    for item in res:
                        if self.robot_parser.can_fetch("*", item):
                            if item not in self.processed:
                                should_insert = True
                                for i in list(self.q.queue):
                                    if item == i[1]:
                                        should_insert = False
                                        break
                                if should_insert and \
                                        (self.current_pages_processed < self.pages_limit or self.pages_limit == 0):
                                    self.q.put((new_depth, item))
                                    add_page_pair_to_database(current_url,item)
                                    self.current_pages_processed += 1
                        else:
                            print("Restricted by robots.txt", item)

            self.q.task_done()
        print(threading.current_thread().name, "is done. Bye-bye")

    def start_crawler(self):
        start = time.time()

        # read robots.txt
        self.robot_parser.set_url("http://" + self.base + "/robots.txt")
        self.robot_parser.read()

        # put first link
        self.q.put((0, self.website))

        threads = []
        for x in range(self.threads_number):
            t = threading.Thread(target=self.worker)
            t.daemon = True
            threads.append(t)
            t.start()

        # wait until the queue becomes empty
        self.q.join()

        # join threads
        for i in range(self.threads_number):
            self.q.put(None)
        for t in threads:
            t.join()

        # empty the queue
        self.q.queue.clear()

        end = time.time()
        print("With", self.threads_number, "threads elapsed : ", end - start)
        print("Total number of pages processed :", self.current_pages_processed)


@crawler_app.get('/crawler')
@crawler_app.post('/crawler')
@view('crawler')
def crawler():
    form = CrawlerFormProcessor(request.forms.decode())
    if request.method == 'POST' and form.validate():
        session.query(Relation).delete()
        session.query(Page).delete()
        session.commit()
        crawl = Crawler(website=form.url.data,
                        depth=3,
                        pages_limit=200,
                        threads_number=8,
                        remove_external_links=not form.uel.data )

        crawl.start_crawler()
        session.commit()
        print("Finish: " + form.url.data)
        redirect("/pages")

    return locals()
