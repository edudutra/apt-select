#!/usr/bin/env python
"""The mirrors module defines classes and methods for Ubuntu archive mirrors.

   Provides latency testing and mirror attribute getting from Launchpad."""

import re
from sys import stderr
from socket import (socket, AF_INET, SOCK_STREAM,
                    gethostbyname, setdefaulttimeout,
                    error, timeout, gaierror)
from time import time
from util_funcs import get_html, HTMLGetError, progress_msg
try:
    from bs4 import BeautifulSoup, FeatureNotFound
except ImportError as err:
    exit((
        "%s\n"
        "Try 'sudo apt-get install python-bs4' "
        "or 'pip install beautifulsoup4'" % err
    ))

try:
    from urlparse import urlparse
except ImportError:
    from urllib.parse import urlparse


class ConnectError(Exception):
    """Socket connection errors"""
    pass


class DataError(Exception):
    """Errors retrieving Launchpad data"""
    pass


class Mirrors(object):
    """Base for collection of archive mirrors"""

    def __init__(self, url_list, flag_ping):
        self.ranked = []
        self.num = len(url_list)
        self.urls = {}
        self.got = {"ping": 0}
        for url in url_list:
            self.urls[url] = {"Host": urlparse(url).netloc}

        if not flag_ping:
            self.got["data"] = 0
            self.status_opts = (
                "unknown",
                "One week behind",
                "Two days behind",
                "One day behind",
                "Up to date"
            )
            self.abort_launch = False
            self.parse_lib = "lxml"
            try:
                BeautifulSoup("", self.parse_lib)
            except FeatureNotFound:
                self.parse_lib = "html.parser"

    def get_launchpad_urls(self):
        """Obtain mirrors' corresponding launchpad URLs"""
        launchpad_base = "https://launchpad.net"
        launchpad_url = launchpad_base + "/ubuntu/+archivemirrors"
        stderr.write("Getting list of launchpad URLs...")
        try:
            launchpad_html = get_html(launchpad_url)
        except HTMLGetError as err:
            stderr.write((
                "%s: %s\nUnable to retrieve list of launchpad sites\n"
                "Reverting to latency only" % (launchpad_url, err)
            ))
            self.abort_launch = True
        else:
            stderr.write("done.\n")
            soup = BeautifulSoup(launchpad_html, self.parse_lib)
            prev = ""
            for element in soup.table.descendants:
                try:
                    url = element.a
                except AttributeError:
                    pass
                else:
                    try:
                        url = url["href"]
                    except TypeError:
                        pass
                    else:

                        if url in self.urls:
                            self.urls[url]["Launchpad"] = launchpad_base + prev

                        if url.startswith("/ubuntu/+mirror/"):
                            prev = url

    def get_rtts(self):
        """Test latency to all mirrors"""
        processed = 0
        stderr.write("Testing %d mirror(s)\n" % self.num)
        progress_msg(processed, self.num)
        for url, info in self.urls.items():
            host = info["Host"]
            try:
                trip = _RoundTrip(host)
            except gaierror as err:
                stderr.write("%s: %s ignored\n" % (err, url))
            else:
                try:
                    rtt = trip.min_rtt()
                except ConnectError as err:
                    stderr.write("\nconnection to %s: %s\n" % (host, err))
                else:
                    self.urls[url].update({"Latency": rtt})
                    self.got["ping"] += 1

            processed += 1
            progress_msg(processed, self.num)

        stderr.write('\n')
        # Mirrors without latency info are removed
        self.urls = {
            key: val for key, val in self.urls.items() if "Latency" in val
        }

        self.ranked = sorted(self.urls, key=lambda x: self.urls[x]["Latency"])

    def lookup_statuses(self, num, min_status, codename, hardware):
        """Scrape requested number of statuses/info from Launchpad"""
        if min_status != "unknown":
            min_index = self.status_opts.index(min_status)
            self.status_opts = self.status_opts[min_index:]

        progress_msg(self.got["data"], num)
        for url in (x for x in self.ranked
                    if "Status" not in self.urls[x]):
            try:
                info = _LaunchData(
                    url, self.urls[url]["Launchpad"],
                    codename, hardware, self.parse_lib
                ).get_info()
            except DataError as err:
                stderr.write("\n%s\n" % err)
            else:
                if info and info[1] and info[1]["Status"] in self.status_opts:
                    self.urls[url].update(info[1])
                    self.got["data"] += 1
                else:
                    self.ranked.remove(info[0])

            progress_msg(self.got["data"], num)
            if self.got["data"] == num:
                break


class _RoundTrip(object):
    """Socket connections for latency reporting"""

    def __init__(self, url):
        self.url = url
        try:
            self.addr = gethostbyname(self.url)
        except gaierror as err:
            raise gaierror(err)

    def __tcp_ping(self):
        """Return socket latency to host's resolved IP address"""
        port = 80
        setdefaulttimeout(2.5)
        sock = socket(AF_INET, SOCK_STREAM)
        send_tstamp = time()*1000
        try:
            sock.connect((self.addr, port))
        except (timeout, error) as err:
            raise ConnectError(err)

        recv_tstamp = time()*1000
        rtt = recv_tstamp - send_tstamp
        sock.close()
        return rtt

    def min_rtt(self):
        """Return lowest rtt"""
        rtts = []
        for _ in range(3):
            try:
                rtt = self.__tcp_ping()
            except ConnectError as err:
                raise ConnectError(err)
            else:
                rtts.append(rtt)

        return round(min(rtts))


class _LaunchData(object):
    """Launchpad mirror data"""

    def __init__(self, url, launch_url, codename, hardware, parse_lib):
        self.url = url
        self.launch_url = launch_url
        self.codename = codename
        self.hardware = hardware
        self.parse_lib = parse_lib

    def get_info(self):
        """Parse launchpad page HTML and place info in queue"""
        try:
            launch_html = get_html(self.launch_url)
        except HTMLGetError as err:
            stderr.write("\nconnection to %s: %s\n" % (self.launch_url, err))
            return None

        info = {}
        soup = BeautifulSoup(launch_html, self.parse_lib)
        for line in soup.find('table', class_='listing sortable',
                              id='arches').find('tbody').find_all('tr'):
            arches = [x.get_text() for x in line.find_all('td')]
            if self.codename in arches[0] and arches[1] == self.hardware:
                info.update({"Status": arches[2]})

        for line in soup.find_all(id=re.compile('speed|organisation')):
            info.update({line.dt.get_text().strip(':'): line.dd.get_text()})

        if "Status" not in info:
            stderr.write((
                "Unable to parse status info from %s\n" % self.launch_url
            ))
            return None

        # Launchpad has more descriptive "unknown" status.
        # It's trimmed here to match statuses list
        if "unknown" in info["Status"]:
            info["Status"] = "unknown"

        return [self.url, info]
