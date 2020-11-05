#!/usr/bin/env/python
# -*- coding: utf-8 -*-

###
# Copyright (c) 2016, Nicolas Coevoet
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
#   * Redistributions of source code must retain the above copyright notice,
#     this list of conditions, and the following disclaimer.
#   * Redistributions in binary form must reproduce the above copyright notice,
#     this list of conditions, and the following disclaimer in the
#     documentation and/or other materials provided with the distribution.
#   * Neither the name of the author of this software nor the name of
#     contributors to this software may be used to endorse or promote products
#     derived from this software without specific prior written consent.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED.  IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

###
# coding: utf-8
import os
import re
import sys
import time
import requests
from urllib.parse import urlencode
import sqlite3
import http.client
import threading
import dns.resolver
import json
import ipaddress
import random
import supybot.log as log
import supybot.conf as conf
import supybot.utils as utils
import supybot.ircdb as ircdb
import supybot.world as world
from supybot.commands import *
import supybot.ircmsgs as ircmsgs
import supybot.plugins as plugins
import supybot.commands as commands
import supybot.ircutils as ircutils
import supybot.callbacks as callbacks
import supybot.schedule as schedule
import supybot.registry as registry
from ftfy.badness import sequence_weirdness
from ftfy.badness import text_cost
try:
    from supybot.i18n import PluginInternationalization
    _ = PluginInternationalization('Sigyn')
except:
    _ = lambda x:x

def repetitions(s):
    # returns a list of (pattern,count), used to detect a repeated pattern inside a single string.
    r = re.compile(r"(.+?)\1+")
    for match in r.finditer(s):
        yield (match.group(1), len(match.group(0))/len(match.group(1)))

def isCloaked (prefix,sig):
    if sig.registryValue('useWhoWas'):
        return False
    if not ircutils.isUserHostmask(prefix):
        return False
    (nick,ident,host) = ircutils.splitHostmask(prefix)
    if '/' in host:
        if host.startswith('gateway/') or host.startswith('nat/'):
            return False
        return True
    return False

def compareString (a,b):
    """return 0 to 1 float percent of similarity ( 0.85 seems to be a good average )"""
    if a == b:
        return 1
    sa, sb = set(a), set(b)
    n = len(sa.intersection(sb))
    if float(len(sa) + len(sb) - n) == 0:
        return 0
    jacc = n / float(len(sa) + len(sb) - n)
    return jacc

def largestString (s1,s2):
    """return largest pattern available in 2 strings"""
    # From https://en.wikibooks.org/wiki/Algorithm_Implementation/Strings/Longest_common_substring#Python2
    # License: CC BY-SA
    m = [[0] * (1 + len(s2)) for i in range(1 + len(s1))]
    longest, x_longest = 0, 0
    for x in range(1, 1 + len(s1)):
        for y in range(1, 1 + len(s2)):
            if s1[x - 1] == s2[y - 1]:
                m[x][y] = m[x - 1][y - 1] + 1
                if m[x][y] > longest:
                    longest = m[x][y]
                    x_longest = x
            else:
                m[x][y] = 0
    return s1[x_longest - longest: x_longest]

def floatToGMT (t):
    f = None
    try:
        f = float(t)
    except:
        return None
    return time.strftime('%Y-%m-%d %H:%M:%S GMT',time.gmtime(f))

def _getRe(f):
    def get(irc, msg, args, state):
        original = args[:]
        s = args.pop(0)
        def isRe(s):
            try:
                foo = f(s)
                return True
            except ValueError:
                return False
        try:
            while len(s) < 512 and not isRe(s):
                s += ' ' + args.pop(0)
            if len(s) < 512:
                state.args.append([s,f(s)])
            else:
                state.errorInvalid('regular expression', s)
        except IndexError:
            args[:] = original
            state.errorInvalid('regular expression', s)
    return get

getPatternAndMatcher = _getRe(utils.str.perlReToPythonRe)

addConverter('getPatternAndMatcher', getPatternAndMatcher)

class Ircd (object):

    __slots__ = ('irc', 'channels','whowas','klines','queues','opered','defcon','pending','logs','limits','netsplit','ping','servers','resolving','stats','patterns','throttled','lastDefcon','god','mx','tokline','toklineresults','dlines', 'invites', 'nicks', 'domains', 'cleandomains', 'ilines', 'klinednicks', 'lastKlineOper')

    def __init__(self,irc):
        self.irc = irc
        # contains Chan instances
        self.channels = {}
        # contains Pattern instances
        self.patterns = {}
        # contains whowas requested for a short period of time
        self.whowas = {}
        # contains klines requested for a short period of time
        self.klines = {}
        # contains various TimeoutQueue for detection purpose
        # often it's [host] { with various TimeOutQueue and others elements }
        self.queues = {}
        # flag or time
        self.opered = False
        # flag or time
        self.defcon = False
        # used for temporary storage of outgoing actions
        self.pending = {}
        self.logs = {}
        # contains servers notices when full or in bad state
        # [servername] = time.time()
        self.limits = {}
        # flag or time
        self.netsplit = time.time() + 300
        self.ping = None
        self.servers = {}
        self.resolving = {}
        self.stats = {}
        self.ilines = {}
        self.throttled = False
        self.lastDefcon = False
        self.god = False
        self.mx = {}
        self.tokline = {}
        self.toklineresults = {}
        self.dlines = []
        self.invites = {}
        self.nicks = {}
        self.domains = {}
        self.cleandomains = {}
        self.klinednicks = utils.structures.TimeoutQueue(86400*2)
        self.lastKlineOper = ''
        try:
            with open('plugins/Sigyn/domains.txt', 'r') as content_file:
                file = content_file.read()
                for line in file.split('\n'):
                    if line.startswith('- '):
                        for word in line.split('- '):
                            self.domains[word.strip().replace("'",'')] = word.strip().replace("'",'')
        except:
            pass

    def __repr__(self):
        return '%s(patterns=%r, queues=%r, channels=%r, pending=%r, logs=%r, limits=%r, whowas=%r, klines=%r)' % (self.__class__.__name__,
        self.patterns, self.queues, self.channels, self.pending, self.logs, self.limits, self.whowas, self.klines)

    def restore (self,db):
        c = db.cursor()
        c.execute("""SELECT id, pattern, regexp, mini, life FROM patterns WHERE removed_at is NULL""")
        items = c.fetchall()
        if len(items):
            for item in items:
                (uid,pattern,regexp,limit,life) = item
                regexp = int(regexp)
                if regexp == 1:
                    regexp = True
                else:
                    regexp = False
                self.patterns[uid] = Pattern(uid,pattern,regexp,limit,life)
        c.close()

    def add (self,db,prefix,pattern,limit,life,regexp):
        c = db.cursor()
        t = 0
        if regexp:
            t = 1
        c.execute("""INSERT INTO patterns VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)""", (pattern,t,limit,life,prefix,'',0,float(time.time())))
        uid = int(c.lastrowid)
        self.patterns[uid] = Pattern(uid,pattern,regexp,limit,life)
        db.commit()
        c.close()
        return uid

    def count(self,db,uid):
        uid = int(uid)
        if uid in self.patterns:
            c = db.cursor()
            c.execute("""SELECT id, triggered FROM patterns WHERE id=? LIMIT 1""",(uid,))
            items = c.fetchall()
            if len(items):
                (uid,triggered) = items[0]
                triggered = int(triggered + 1)
                c.execute("""UPDATE patterns SET triggered=? WHERE id=?""",(triggered,uid))
                db.commit()
            c.close()

    def ls (self,db,pattern,deep=False):
        c = db.cursor()
        glob = '*%s*' % pattern
        like = '%'+pattern+'%'
        i = None
        try:
            i = int(pattern)
        except:
            i = None
        if i:
            c.execute("""SELECT id, pattern, regexp, operator, at, triggered, removed_at, removed_by, comment, mini, life FROM patterns WHERE id=? LIMIT 1""",(i,))
        else:
            if deep:
                c.execute("""SELECT id, pattern, regexp, operator, at, triggered, removed_at, removed_by, comment, mini, life FROM patterns WHERE id GLOB ? OR id LIKE ? OR pattern GLOB ? OR pattern LIKE ? OR comment GLOB ? OR comment LIKE ? ORDER BY id DESC""",(glob,like,glob,like,glob,like))
            else:
                c.execute("""SELECT id, pattern, regexp, operator, at, triggered, removed_at, removed_by, comment, mini, life FROM patterns WHERE (id GLOB ? OR id LIKE ? OR pattern GLOB ? OR pattern LIKE ? OR comment GLOB ? OR comment LIKE ?) and removed_at is NULL ORDER BY id DESC""",(glob,like,glob,like,glob,like))
        items = c.fetchall()
        c.close()
        if len(items):
            results = []
            for item in items:
                (uid,pattern,regexp,operator,at,triggered,removed_at,removed_by,comment,limit,life) = item
                end = ''
                if i:
                    if removed_by:
                        end = ' - disabled on %s by %s - ' % (floatToGMT(removed_at),removed_by.split('!')[0])
                    regexp = int(regexp)
                    reg = 'not case sensitive'
                    if regexp == 1:
                        reg = 'regexp pattern'
                    results.append('#%s "%s" by %s on %s (%s calls) %s/%ss%s %s - %s' % (uid,pattern,operator.split('!')[0],floatToGMT(at),triggered,limit,life,end,comment,reg))
                else:
                    if removed_by:
                        end = ' (disabled)'
                    results.append('[#%s "%s" (%s calls) %s/%ss%s]' % (uid,pattern,triggered,limit,life,end))
            return results
        return []

    def edit (self,db,uid,limit,life,comment):
        c = db.cursor()
        uid = int(uid)
        c.execute("""SELECT id, life FROM patterns WHERE id=? LIMIT 1""",(uid,))
        items = c.fetchall()
        if len(items):
            if comment:
                c.execute("""UPDATE patterns SET life=?, mini=?, comment=? WHERE id=? LIMIT 1""",(life,limit,comment,uid))
            else:
                c.execute("""UPDATE patterns SET life=?, mini=? WHERE id=? LIMIT 1""",(life,limit,uid))
            db.commit()
            if uid in self.patterns:
               self.patterns[uid].life = life
               self.patterns[uid].limit = limit
            found = True
        c.close()
        return (len(items))

    def toggle (self,db,uid,prefix,active):
        c = db.cursor()
        uid = int(uid)
        c.execute("""SELECT id, pattern, regexp, mini, life, removed_at, removed_by FROM patterns WHERE id=? LIMIT 1""",(uid,))
        items = c.fetchall()
        updated = False
        if len(items):
            (id,pattern,regexp,limit,life,removed_at,removed_by) = items[0]
            regexp = int(regexp)
            if active and removed_at:
                c.execute("""UPDATE patterns SET removed_at=NULL, removed_by=NULL WHERE id=? LIMIT 1""",(uid,))
                self.patterns[uid] = Pattern(uid,pattern,regexp == 1,limit,life)
                updated = True
            elif not removed_at and not active:
                c.execute("""UPDATE patterns SET removed_at=?, removed_by=? WHERE id=? LIMIT 1""",(float(time.time()),prefix,uid))
                if uid in self.patterns:
                    del self.patterns[uid]
                updated = True
            db.commit()
        c.close()
        return updated

    def remove (self, db, uid):
        c = db.cursor()
        uid = int(uid)
        c.execute("""SELECT id, pattern, regexp, mini, life, removed_at, removed_by FROM patterns WHERE id=? LIMIT 1""",(uid,))
        items = c.fetchall()
        updated = False
        if len(items):
            (id,pattern,regexp,limit,life,removed_at,removed_by) = items[0]
            c.execute("""DELETE FROM patterns WHERE id=? LIMIT 1""",(uid,))
            if not removed_at:
                if uid in self.patterns:
                    del self.patterns[uid]
            updated = True
            db.commit()
        c.close()
        return updated

class Chan (object):
    __slots__ = ('channel', 'patterns', 'buffers', 'logs', 'nicks', 'called', 'klines', 'requestedBySpam')
    def __init__(self,channel):
        self.channel = channel
        self.patterns = None
        self.buffers = {}
        self.logs = {}
        self.nicks = {}
        self.called = False
        self.klines = utils.structures.TimeoutQueue(1800)
        self.requestedBySpam = False

    def __repr__(self):
        return '%s(channel=%r, patterns=%r, buffers=%r, logs=%r, nicks=%r)' % (self.__class__.__name__,
        self.channel, self.patterns, self.buffers, self.logs, self.nicks)

class Pattern (object):
    __slots__ = ('uid', 'pattern', 'limit', 'life', '_match')
    def __init__(self,uid,pattern,regexp,limit,life):
        self.uid = uid
        self.pattern = pattern
        self.limit = limit
        self.life = life
        self._match = False
        if regexp:
            self._match = utils.str.perlReToPythonRe(pattern)
        else:
            self.pattern = pattern.lower()

    def match (self,text):
        s = False
        if isinstance(text,bytes):
            text = str(text, "utf-8")
        if self._match:
            s = self._match.search (text) != None
        else:
            text = text.lower()
            s = self.pattern in text
        return s

    def __repr__(self):
        return '%s(uid=%r, pattern=%r, limit=%r, life=%r, _match=%r)' % (self.__class__.__name__,
        self.uid, self.pattern, self.limit, self.life, self._match)

class Sigyn(callbacks.Plugin,plugins.ChannelDBHandler):
    """Network and Channels Spam protections"""
    threaded = True
    noIgnore = True

    def __init__(self, irc):
        callbacks.Plugin.__init__(self, irc)
        plugins.ChannelDBHandler.__init__(self)
        self._ircs = ircutils.IrcDict()
        self.cache = {}
        self.getIrc(irc)
        self.starting = world.starting
        self.recaps = re.compile("[A-Z]")
        self.ipfiltered = {}
        self.rmrequestors = {}
        self.spamchars = {'Ḕ', 'Î', 'Ù', 'Ṋ', 'ℰ', 'Ừ', 'ś', 'ï', 'ℯ', 'ļ', 'ẋ', 'ᾒ', 'ἶ', 'ệ', 'ℓ', 'Ŋ', 'Ḝ', 'ξ', 'ṵ', 'û', 'ẻ', 'Ũ', 'ṡ', '§', 'Ƚ', 'Š', 'ᶙ', 'ṩ', '¹', 'ư', 'Ῐ', 'Ü', 'ŝ', 'ὴ', 'Ș', 'ũ', 'ῑ', 'ⱷ', 'Ǘ', 'Ɇ', 'ĭ', 'ἤ', 'Ɲ', 'Ǝ', 'ủ', 'µ', 'Ỵ', 'Ű', 'ū', 'į', 'ἳ', 'ΐ', 'ḝ', 'Ɛ', 'ṇ', 'È', 'ῆ', 'ử', 'Ň', 'υ', 'Ǜ', 'Ἔ', 'Ὑ', 'μ', 'Ļ', 'ů', 'Ɫ', 'ŷ', 'Ǚ', 'ἠ', 'Ĺ', 'Ę', 'Ὲ', 'Ẍ', 'Ɣ', 'Ϊ', 'ℇ', 'ẍ', 'ῧ', 'ϵ', 'ἦ', 'ừ', 'ṳ', 'ᾕ', 'ṋ', 'ù', 'ῦ', 'Ι', 'ῠ', 'ṥ', 'ὲ', 'ê', 'š', 'ě', 'ề', 'ẽ', 'ī', 'Ė', 'ỷ', 'Ủ', 'ḯ', 'Ἓ', 'Ὓ', 'Ş', 'ύ', 'Ṧ', 'Ŷ', 'ἒ', 'ἵ', 'ė', 'ἰ', 'ẹ', 'Ȇ', 'Ɏ', 'Ί', 'ὶ', 'Ε', 'ḛ', 'Ὤ', 'ǐ', 'ȇ', 'ἢ', 'í', 'ȕ', 'Ữ', '＄', 'ή', 'Ṡ', 'ἷ', 'Ḙ', 'Ὢ', 'Ṉ', 'Ľ', 'ῃ', 'Ụ', 'Ṇ', 'ᾐ', 'Ů', 'Ἕ', 'ý', 'Ȅ', 'ᴌ', 'ύ', 'ņ', 'ὒ', 'Ý', 'ế', 'ĩ', 'ǘ', 'Ē', 'ṹ', 'Ư', 'é', 'Ÿ', 'ΰ', 'Ὦ', 'Ë', 'ỳ', 'ἓ', 'ĕ', 'ἑ', 'ṅ', 'ȗ', 'Ν', 'ί', 'ể', 'ᴟ', 'è', 'ᴇ', 'ḭ', 'ȝ', 'ϊ', 'ƪ', 'Ὗ', 'Ų', 'Ề', 'Ṷ', 'ü', 'Ɨ', 'Ώ', 'ň', 'ṷ', 'ƞ', 'Ȗ', 'ș', 'ῒ', 'Ś', 'Ự', 'Ń', 'Ἳ', 'Ứ', 'Ἷ', 'ἱ', 'ᾔ', 'ÿ', 'Ẽ', 'ὖ', 'ὑ', 'ἧ', 'Ὥ', 'ṉ', 'Ὠ', 'ℒ', 'Ệ', 'Ὼ', 'Ẻ', 'ḙ', 'Ŭ', '₴', 'Ὡ', 'ȉ', 'Ṅ', 'ᵪ', 'ữ', 'Ὧ', 'ń', 'Ἐ', 'Ú', 'ɏ', 'î', 'Ⱡ', 'Ƨ', 'Ě', 'ȿ', 'ᴉ', 'Ṩ', 'Ê', 'ȅ', 'ᶊ', 'Ṻ', 'Ḗ', 'ǹ', 'ᴣ', 'ş', 'Ï', 'ᾗ', 'ự', 'ὗ', 'ǔ', 'ᶓ', 'Ǹ', 'Ἶ', 'Ṳ', 'Ʊ', 'ṻ', 'Ǐ', 'ᵴ', 'ῇ', 'Ẹ', 'Ế', 'Ϋ', 'Ū', 'Ῑ', 'ί', 'ỹ', 'Ḯ', 'ǀ', 'Ὣ', 'Ȳ', 'ǃ', 'ų', 'ϴ', 'Ώ', 'Í', 'ì', 'ι', 'ῄ', 'ΰ', 'ἣ', 'ῡ', 'Ἒ', 'Ḽ', 'Ȉ', 'Έ', 'ἴ', 'ᶇ', 'ἕ', 'ǚ', 'Ī', 'Έ', '¥', 'Ṵ', 'ὔ', 'Ŝ', 'ῢ', 'Ἱ', 'ű', 'Ḷ', 'Ὶ', 'ḗ', 'ᴜ', 'ę', 'ὐ', 'Û', 'ᾑ', 'Ʋ', 'Ἑ', 'Ì', 'ŋ', 'Ḛ', 'ỵ', 'Ễ', '℮', '×', 'Ῠ', 'Ἵ', 'Ύ', 'Ử', 'ᴈ', 'ē', 'Ἰ', 'ᶖ', 'ȳ', 'Ǯ', 'ὓ', 'ὕ', 'ῂ', 'Ĕ', 'É', 'ᾓ', 'Ḻ', 'Ņ', 'ἥ', 'ḕ', 'ὺ', 'Ȋ', 'ı', 'Ȕ', 'ṧ', 'ᾖ', 'Ί', 'ΐ', '€', 'Ḭ', 'Ƴ', 'ȵ', 'Ṹ', 'Ñ', 'Ƞ', 'Ȩ', 'ῐ', 'ứ', 'έ', 'ł', 'ŭ', '϶', 'ƴ', '₤', 'ƨ', '£', 'Ł', 'ñ', 'ë', 'ễ', 'ǯ', 'ᶕ', 'ή', 'ᶔ', 'Π', 'ȩ', 'ἐ', 'Ể', 'ε', 'Ĩ', 'ǜ', 'Į', 'Ξ', 'Ḹ', 'Ῡ', '∩', 'ú', 'Χ', 'ụ'}

    def removeDnsbl (self,irc,ip,droneblHost,droneblKey):
        headers = {
            'Content-Type' : 'text/xml'
        }
        def check(answer):
            found = False
            for line in answer.split('\n'):
                if line.find('listed="1"') != -1:
                    id = line.split('id="')[1]
                    id = id.split('"')[0]
                    if line.find('type="18"') != -1:
                        self.logChannel(irc,'RMDNSBL: %s (%s) not removed: is type 18' % (ip,id))
                        if ip in self.rmrequestors:
                            irc.queueMsg(ircmsgs.privmsg(self.rmrequestors[ip],'%s (%s) not removed: is type 18' % (ip,id)))
                            del self.rmrequestors[ip]
                        continue
                    data = "<?xml version=\"1.0\"?><request key='"+droneblKey+"'><remove id='"+id+"' /></request>"
                    found = True
                    try:
                        r = requests.post(droneblHost,data=data,headers=headers)
                        self.logChannel(irc,'RMDNSBL: %s (%s) removed' % (ip,id))
                        if ip in self.rmrequestors:
                            irc.queueMsg(ircmsgs.privmsg(self.rmrequestors[ip],'%s (%s) removed' % (ip,id)))
                            del self.rmrequestors[ip]
                    except:
                        self.logChannel(irc,'RMDNSBL: %s (%s) failed: unknown error' % (ip,id))
                        if ip in self.rmrequestors:
                            irc.queueMsg(ircmsgs.privmsg(self.rmrequestors[ip],'%s (%s) not removed: unknown error' % (ip,id)))
                            del self.rmrequestors[ip]
            if not found:
                self.logChannel(irc,'RMDNSBL: %s (none) not removed: no listing found' % ip)
                if ip in self.rmrequestors:
                    irc.queueMsg(ircmsgs.privmsg(self.rmrequestors[ip],'%s (%s) not removed: no listing found' % (ip,id)))
                    del self.rmrequestors[ip]
        data = '<?xml version="1.0"?><request key="%s"><lookup ip="%s" own="1" /></request>' % (
            droneblKey, ip
        )
        r = requests.post(droneblHost,data=data,headers=headers)
        if r.status_code == 200:
            check(r.text)
        else:
            self.logChannel(irc,'RMDNSBL: %s (unknown) failed: status code %s' % (ip,r.status_code))
            if ip in self.rmrequestors:
                irc.queueMsg(ircmsgs.privmsg(self.rmrequestors[ip],'%s (unknown) not removed: status code %s' % (ip,r.status_code)))

    def fillDnsbl (self,irc,ip,droneblHost,droneblKey,comment=None):
        headers = {
            'Content-Type' : 'text/xml'
        }
        def check(answer):
            self.log.info ('fillDnsbl, answered %s' % ip)
            if 'listed="1"' in answer:
                self.logChannel(irc,'DNSBL: %s (already listed)' % ip)
                return
            type = 3
            if comment == 'Bottler':
                type = 5
            elif comment == 'Unknown spambot or drone':
                type = 6
            elif comment == 'DDOS Drone':
                type = 7
            elif comment == 'SOCKS Proxy':
                type = 8
            elif comment == 'HTTP Proxy':
                type = 9
            elif comment == 'ProxyChain':
                type = 10
            elif comment == 'Web Page Proxy':
                type = 11
            elif comment == 'Open DNS Resolver':
                type = 12
            elif comment == 'Brute force attackers':
                type = 13
            elif comment == 'Open Wingate Proxy':
                type = 14
            elif comment == 'Compromised router / gateway':
                type = 15
            elif comment == 'Autorooting worms':
                type = 16
            elif comment == 'Automatically determined botnet IPs (experimental)':
                type = 17
            elif comment == 'DNS/MX type hostname detected on IRC':
                type = 18
            elif comment == "Abused VPN Service":
                type = 19
            data = "<?xml version=\"1.0\"?><request key='"+droneblKey+"'><add ip='"+ip+"' type='"+str(type)+"' comment='used by irc spam bot' /></request>"
            r = requests.post(droneblHost,data=data,headers=headers)
            if r.status_code != 200:
                self.logChannel(irc,'DNSBL: %s (add returned %s %s)' % (ip,r.status_code,r.reason))
            if comment:
                self.logChannel(irc,'DNSBL: %s (%s,type:%s)' % (ip,comment,type))
            else:
                self.logChannel(irc,'DNSBL: %s' % ip)
        self.log.info('fillDnsbl, checking %s' % ip)
        data = "<?xml version=\"1.0\"?><request key='"+droneblKey+"'><lookup ip='"+ip+"' /></request>"
        try:
            r = requests.post(droneblHost,data=data,headers=headers,timeout=9)
            if r.status_code == 200:
                check(r.text)
            else:
                self.logChannel(irc,'DNSBL: %s (%s)' % (ip,r.status_code))
        except requests.exceptions.RequestException as e:
            self.logChannel(irc,'DNSBL: %s (%s)' % (ip,e))

    def state (self,irc,msg,args,channel):
        """[<channel>]

        returns state of the plugin, for optional <channel>"""
        self.cleanup(irc)
        i = self.getIrc(irc)
        if not channel:
            irc.queueMsg(ircmsgs.privmsg(msg.nick,'Opered %s, enable %s, defcon %s, netsplit %s' % (i.opered,self.registryValue('enable'),(i.defcon),i.netsplit)))
            irc.queueMsg(ircmsgs.privmsg(msg.nick,'There are %s permanent patterns and %s channels directly monitored' % (len(i.patterns),len(i.channels))))
            channels = 0
            prefixs = 0
            for k in i.queues:
                if irc.isChannel(k):
                    channels += 1
                elif ircutils.isUserHostmask(k):
                    prefixs += 1
            irc.queueMsg(ircmsgs.privmsg(msg.nick,"Via server's notices: %s channels and %s users monitored" % (channels,prefixs)))
        for chan in i.channels:
            if channel == chan:
                ch = self.getChan(irc,chan)
                if not self.registryValue('ignoreChannel',channel=chan):
                    called = ""
                    if ch.called:
                        called = 'currently in defcon'
                    irc.queueMsg(ircmsgs.privmsg(msg.nick,'On %s (%s users) %s:' % (chan,len(ch.nicks),called)))
                    protections = ['flood','lowFlood','repeat','lowRepeat','massRepeat','lowMassRepeat','hilight','nick','ctcp']
                    for protection in protections:
                        if self.registryValue('%sPermit' % protection,channel=chan) > -1:
                            permit = self.registryValue('%sPermit' % protection,channel=chan)
                            life = self.registryValue('%sLife' % protection,channel=chan)
                            abuse = self.hasAbuseOnChannel(irc,chan,protection)
                            if abuse:
                                abuse = ' (ongoing abuses) '
                            else:
                                abuse = ''
                            count = 0
                            if protection == 'repeat':
                                for b in ch.buffers:
                                    if ircutils.isUserHostmask('n!%s' % b):
                                        count += 1
                            else:
                                for b in ch.buffers:
                                    if protection in b:
                                        count += len(ch.buffers[b])
                            if count:
                                count = " - %s user's buffers" % count
                            else:
                                count = ""
                            irc.queueMsg(ircmsgs.privmsg(msg.nick," - %s : %s/%ss %s%s" % (protection,permit,life,abuse,count)))
        irc.replySuccess()
    state = wrap(state,['owner',optional('channel')])

    def defcon (self,irc,msg,args,channel):
        """[<channel>]

        limits are lowered, globally or for a specific <channel>"""
        i = self.getIrc(irc)
        if channel and channel != self.registryValue('logChannel'):
            if channel in i.channels and self.registryValue('abuseDuration',channel=channel) > 0:
                chan = self.getChan(irc,channel)
                if chan.called:
                    self.logChannel(irc,'INFO: [%s] rescheduled ignores lifted, limits lowered (by %s) for %ss' % (channel,msg.nick,self.registryValue('abuseDuration',channel=channel)))
                    chan.called = time.time()
                else:
                    self.logChannel(irc,'INFO: [%s] ignores lifted, limits lowered (by %s) for %ss' % (channel,msg.nick,self.registryValue('abuseDuration',channel=channel)))
                    chan.called = time.time()
        else:
            if i.defcon:
                i.defcon = time.time()
                irc.reply('Already in defcon mode, reset, %ss more' % self.registryValue('defcon'))
            else:
                i.defcon = time.time()
                self.logChannel(irc,"INFO: ignores lifted and abuses end to klines for %ss by %s" % (self.registryValue('defcon'),msg.nick))
                if not i.god:
                    irc.sendMsg(ircmsgs.IrcMsg('MODE %s +p' % irc.nick))
                else:
                    self.applyDefcon (irc)
        irc.replySuccess()
    defcon = wrap(defcon,['owner',optional('channel')])

    def vacuum (self,irc,msg,args):
        """takes no arguments

        VACUUM the permanent patterns's database"""
        db = self.getDb(irc.network)
        c = db.cursor()
        c.execute('VACUUM')
        c.close()
        irc.replySuccess()
    vacuum = wrap(vacuum,['owner'])

    def leave (self,irc,msg,args,channel):
       """<channel>

       force the bot to part <channel> and won't rejoin even if invited
       """
       if channel in irc.state.channels:
           reason = conf.supybot.plugins.channel.partMsg.getValue()
           irc.queueMsg(ircmsgs.part(channel,reason))
           try:
               network = conf.supybot.networks.get(irc.network)
               network.channels().remove(channel)
           except:
               pass
       self.setRegistryValue('lastActionTaken',-1.0,channel=channel)
       irc.replySuccess()
    leave = wrap(leave,['owner','channel'])

    def stay (self,irc,msg,args,channel):
       """<channel>

       force bot to stay in <channel>
       """
       self.setRegistryValue('leaveChannelIfNoActivity',-1,channel=channel)
       if not channel in irc.state.channels:
           self.setRegistryValue('lastActionTaken',time.time(),channel=channel)
           irc.queueMsg(ircmsgs.join(channel))
           try:
               network = conf.supybot.networks.get(irc.network)
               network.channels().add(channel)
           except KeyError:
               pass
       irc.replySuccess()
    stay = wrap(stay,['owner','channel'])

    def isprotected (self,irc,msg,args,hostmask,channel):
        """<hostmask> [<channel>]

        returns true if <hostmask> is protected, in optional <channel>"""
        if ircdb.checkCapability(hostmask, 'protected'):
            irc.reply('%s is globally protected' % hostmask)
        else:
            if channel:
                protected = ircdb.makeChannelCapability(channel, 'protected')
                if ircdb.checkCapability(hostmask, protected):
                    irc.reply('%s is protected in %s' % (hostmask,channel))
                else:
                    irc.reply('%s is not protected in %s' % (hostmask,channel))
            else:
                irc.reply('%s is not protected' % hostmask);
    isprotected = wrap(isprotected,['owner','hostmask',optional('channel')])

    def checkactions (self,irc,msg,args,duration):
        """<duration> in days

        return channels where last action taken is older than <duration>"""
        channels = []
        duration = duration * 24 * 3600
        for channel in irc.state.channels:
            if irc.isChannel(channel):
                if self.registryValue('mainChannel') in channel or channel == self.registryValue('reportChannel') or self.registryValue('snoopChannel') == channel or self.registryValue('secretChannel') == channel:
                    continue
                if self.registryValue('ignoreChannel',channel):
                    continue
                action = self.registryValue('lastActionTaken',channel=channel)
                if action > 0:
                    if time.time()-action > duration:
                        channels.append('%s: %s' % (channel,time.strftime('%Y-%m-%d %H:%M:%S GMT',time.gmtime(action))))
                else:
                    channels.append(channel)
        irc.replies(channels,None,None,False)
    checkactions = wrap(checkactions,['owner','positiveInt'])

    def netsplit (self,irc,msg,args,duration):
        """<duration>

         entering netsplit mode for <duration> (in seconds)"""
        i = self.getIrc(irc)
        if i.netsplit:
            i.netsplit = time.time()+duration
            irc.reply('Already in netsplit mode, reset, %ss more' % duration)
        else:
            i.netsplit = time.time()+duration
            self.logChannel(irc,"INFO: netsplit activated for %ss by %s: some abuses are ignored" % (duration,msg.nick))
            irc.replySuccess()
    netsplit = wrap(netsplit,['owner','positiveInt'])

    def checkpattern (self,irc,msg,args,text):
        """ <text>

        returns permanents patterns triggered by <text>"""
        i = self.getIrc(irc)
        patterns = []
        text = text.encode('utf-8').strip()
        for k in i.patterns:
            pattern = i.patterns[k]
            if pattern.match(text):
                patterns.append('#%s' % pattern.uid)
        if len(patterns):
            irc.queueMsg(ircmsgs.privmsg(msg.nick,'%s matches: %s' % (len(patterns),', '.join(patterns))))
        else:
            irc.reply('No matches')
    checkpattern = wrap(checkpattern,['owner','text'])

    def lspattern (self,irc,msg,args,optlist,pattern):
        """[--deep] <id|pattern>

        returns patterns which matches pattern or info about pattern #id, use --deep to search on deactivated patterns, * to return all pattern"""
        i = self.getIrc(irc)
        deep = pattern == '*'
        for (option, arg) in optlist:
            if option == 'deep':
                deep = True
        results = i.ls(self.getDb(irc.network),pattern,deep)
        if len(results):
            if deep or pattern == '*':
                for r in results:
                    irc.queueMsg(ircmsgs.privmsg(msg.nick,r))
            else:
                irc.replies(results,None,None,False)
        else:
            irc.reply('no pattern found')
    lspattern = wrap(lspattern,['owner',getopts({'deep': ''}),'text'])

    def rmpattern (self,irc,msg,args,ids):
        """<id> [<id>]

        remove permanent pattern by id"""
        i = self.getIrc(irc)
        results = []
        for id in ids:
            result = i.remove(self.getDb(irc.network),id)
            if result:
                results.append('#%s' % id)
        self.logChannel(irc,'PATTERN: %s deleted %s' % (msg.nick,','.join(results)))
        irc.replySuccess()
    rmpattern = wrap(rmpattern,['owner',many('positiveInt')])

    def addpattern (self,irc,msg,args,limit,life,pattern):
        """<limit> <life> <pattern>

        add a permanent <pattern> : kline after <limit> calls raised during <life> seconds,
        for immediate kline use limit 0"""
        i = self.getIrc(irc)
        pattern = pattern.lower()
        result = i.add(self.getDb(irc.network),msg.prefix,pattern,limit,life,False)
        self.logChannel(irc,'PATTERN: %s added #%s : "%s" %s/%ss' % (msg.nick,result,pattern,limit,life))
        irc.reply('#%s added' % result)
    addpattern = wrap(addpattern,['owner','nonNegativeInt','positiveInt','text'])

    def addregexpattern (self,irc,msg,args,limit,life,pattern):
        """<limit> <life> /<pattern>/

        add a permanent /<pattern>/ to kline after <limit> calls raised during <life> seconds,
        for immediate kline use limit 0"""
        i = self.getIrc(irc)
        result = i.add(self.getDb(irc.network),msg.prefix,pattern[0],limit,life,True)
        self.logChannel(irc,'PATTERN: %s added #%s : "%s" %s/%ss' % (msg.nick,result,pattern[0],limit,life))
        irc.reply('#%s added' % result)
    addregexpattern = wrap(addregexpattern,['owner','nonNegativeInt','positiveInt','getPatternAndMatcher'])

    def editpattern (self,irc,msg,args,uid,limit,life,comment):
        """<id> <limit> <life> [<comment>]

        edit #<id> with new <limit> <life> and <comment>"""
        i = self.getIrc(irc)
        result = i.edit(self.getDb(irc.network),uid,limit,life,comment)
        if result:
            if comment:
                self.logChannel(irc,'PATTERN: %s edited #%s with %s/%ss (%s)' % (msg.nick,uid,limit,life,comment))
            else:
                self.logChannel(irc,'PATTERN: %s edited #%s with %s/%ss' % (msg.nick,uid,limit,life))
            irc.replySuccess()
        else:
            irc.reply("#%s doesn't exist")
    editpattern = wrap(editpattern,['owner','positiveInt','nonNegativeInt','positiveInt',optional('text')])

    def togglepattern (self,irc,msg,args,uid,toggle):
        """<id> <boolean>

        activate or deactivate #<id>"""
        i = self.getIrc(irc)
        result = i.toggle(self.getDb(irc.network),uid,msg.prefix,toggle)
        if result:
            if toggle:
                self.logChannel(irc,'PATTERN: %s enabled #%s' % (msg.nick,uid))
            else:
                self.logChannel(irc,'PATTERN: %s disabled #%s' % (msg.nick,uid))
            irc.replySuccess()
        else:
            irc.reply("#%s doesn't exist or is already in requested state" % uid)
    togglepattern = wrap(togglepattern,['owner','positiveInt','boolean'])

    def lstmp (self,irc,msg,args,channel):
        """[<channel>]

        returns temporary patterns for given channel"""
        i = self.getIrc(irc)
        if channel in i.channels:
            chan = self.getChan(irc,channel)
            if chan.patterns:
                patterns = list(chan.patterns)
                if len(patterns):
                    irc.reply('[%s] %s patterns : %s' % (channel,len(patterns),', '.join(patterns)))
                else:
                   irc.reply('[%s] no active pattern' % channel)
            else:
                irc.reply('[%s] no active pattern' % channel)
        else:
            irc.reply('[%s] is unknown' % channel)
    lstmp = wrap(lstmp,['op'])

    def dnsblresolve (self,irc,msg,args,ips):
        """<ip> [,<ip>]

            add <ips> on dronebl, hostmasks can be provided"""
        for ip in ips:
            if utils.net.isIPV4(ip) or utils.net.bruteIsIPV6(ip):
                t = world.SupyThread(target=self.fillDnsbl,name=format('fillDnsbl %s', ip),args=(irc,ip,self.registryValue('droneblHost'),self.registryValue('droneblKey'),"Unknown spambot or drone"))
                t.setDaemon(True)
                t.start()
            else:
                prefix = "*!*@%s" % ip
                if ircutils.isUserHostmask(prefix):
                    t = world.SupyThread(target=self.resolve,name=format('resolve %s', prefix),args=(irc,prefix,'',True,"Unknown spambot or drone"))
                    t.setDaemon(True)
                    t.start()
        irc.replySuccess()
    dnsblresolve = wrap(dnsblresolve,['owner',commalist('something')])

    def dnsbl (self,irc,msg,args,ips,comment):
       """<ip> [,<ip>] [<comment>]

          add <ips> on dronebl, <comment> can be used to change type (Bottler|Unknown spambot or drone|DDOS Drone|SOCKS Proxy|HTTP Proxy|ProxyChain|Web Page Proxy|Open DNS Resolver|Brute force attackers|Open Wingate Proxy|Compromised router / gateway|Autorooting worms)"""
       for ip in ips:
           if utils.net.isIPV4(ip) or utils.net.bruteIsIPV6(ip):
               t = world.SupyThread(target=self.fillDnsbl,name=format('fillDnsbl %s', ip),args=(irc,ip,self.registryValue('droneblHost'),self.registryValue('droneblKey'),comment))
               t.setDaemon(True)
               t.start()
       irc.replySuccess()
    dnsbl = wrap(dnsbl,['owner',commalist('ip'),rest('text')])

    def rmdnsbl (self,irc,msg,args,ips):
        """<ip> [<ip>]

           remove <ips> from dronebl"""
        for ip in ips:
            if utils.net.isIPV4(ip) or utils.net.bruteIsIPV6(ip):
                self.rmrequestors[ip] = msg.nick
                t = world.SupyThread(target=self.removeDnsbl,name=format('rmDnsbl %s', ip),args=(irc,ip,self.registryValue('droneblHost'),self.registryValue('droneblKey')))
                t.setDaemon(True)
                t.start()
        irc.replySuccess()
    rmdnsbl = wrap(rmdnsbl,['owner',many('ip')])

    def addtmp (self,irc,msg,args,channel,text):
        """[<channel>] <message>

        add a string in channel's temporary patterns"""
        text = text.lower()
        i = self.getIrc(irc)
        if channel in i.channels:
            chan = self.getChan(irc,channel)
            shareID = self.registryValue('shareComputedPatternID',channel=channel)
            if shareID == -1 or not i.defcon:
                life = self.registryValue('computedPatternLife',channel=channel)
                if not chan.patterns:
                    chan.patterns = utils.structures.TimeoutQueue(life)
                elif chan.patterns.timeout != life:
                    chan.patterns.setTimeout(life)
                chan.patterns.enqueue(text)
                self.logChannel(irc,'PATTERN: [%s] added tmp "%s" for %ss by %s' % (channel,text,life,msg.nick))
                irc.replySuccess()
            else:
                n = 0
                l = self.registryValue('computedPatternLife',channel=channel)
                for channel in i.channels:
                    chan = self.getChan(irc,channel)
                    id = self.registryValue('shareComputedPatternID',channel=channel)
                    if id == shareID:
                        life = self.registryValue('computedPatternLife',channel=channel)
                        if not chan.patterns:
                            chan.patterns = utils.structures.TimeoutQueue(life)
                        elif chan.patterns.timeout != life:
                            chan.patterns.setTimeout(life)
                        chan.patterns.enqueue(text)
                        n = n + 1
                self.logChannel(irc,'PATTERN: added tmp "%s" for %ss by %s in %s channels' % (text,l,msg.nick,n))
                irc.replySuccess()
        else:
            irc.reply('unknown channel')
    addtmp = wrap(addtmp,['op','text'])

    def addglobaltmp (self,irc,msg,args,text):
        """<text>

        add <text> to temporary patterns in all channels"""
        text = text.lower()
        i = self.getIrc(irc)
        n = 0
        for channel in i.channels:
            chan = self.getChan(irc,channel)
            life = self.registryValue('computedPatternLife',channel=channel)
            if not chan.patterns:
                chan.patterns = utils.structures.TimeoutQueue(life)
            elif chan.patterns.timeout != life:
                chan.patterns.setTimeout(life)
            chan.patterns.enqueue(text)
            n = n + 1
        self.logChannel(irc,'PATTERN: added tmp "%s" for %ss by %s in %s channels' % (text,life,msg.nick,n))
        irc.replySuccess()
    addglobaltmp = wrap(addglobaltmp,['owner','text'])

    def rmtmp (self,irc,msg,args,channel):
        """[<channel>]

        remove temporary patterns for given channel"""
        i = self.getIrc(irc)
        if channel in i.channels:
            chan = self.getChan(irc,channel)
            shareID = self.registryValue('shareComputedPatternID',channel=channel)
            if shareID != -1:
                n = 0
                for channel in i.channels:
                    id = self.registryValue('shareComputedPatternID',channel=channel)
                    if id == shareID:
                       if i.channels[channel].patterns:
                           i.channels[channel].patterns.reset()
                           n = n + 1
                self.logChannel(irc,'PATTERN: removed tmp patterns in %s channels by %s' % (n,msg.nick))
            elif chan.patterns:
                l = len(chan.patterns)
                chan.patterns.reset()
                if l:
                    self.logChannel(irc,'PATTERN: [%s] removed %s tmp pattern by %s' % (channel,l,msg.nick))
                    irc.replySuccess()
                else:
                    irc.reply('[%s] no active pattern' % channel)
            else:
                irc.reply('[%s] no active pattern' % channel)
        else:
            irc.reply('unknown channel')
    rmtmp = wrap(rmtmp,['op'])

    def unkline (self,irc,msg,args,nick):
       """<nick>
          request unkline of <nick>, klined recently from your channel
       """
       channels = []
       ops = []
       nick = nick.lower()
       for channel in irc.state.channels:
           if msg.nick in irc.state.channels[channel].ops:
               chan = self.getChan(irc,channel)
               if len(chan.klines):
                   for q in chan.klines:
                       self.log.info('klines found %s' % q)
                       if q.startswith(nick):
                          ip = q.split(' ')[1]
                          channels.append(channel)
                          if not isCloaked('%s!%s' % (nick,ip),self):
                              if self.registryValue('useOperServ'):
                                  irc.sendMsg(ircmsgs.IrcMsg('PRIVMSG OperServ :AKILL DEL %s' % ip))
                              else:
                                  irc.queueMsg(ircmsgs.IrcMsg('UNKLINE %s' % ip))
                              if self.registryValue('clearTmpPatternOnUnkline',channel=channel):
                                  if chan.patterns and len(chan.patterns):
                                      self.logChannel(irc,'PATTERN: [%s] removed %s tmp pattern by %s' % (channel,len(chan.patterns),msg.nick))
                                      chan.patterns.reset()
                              self.logChannel(irc,'OP: [%s] %s unklined %s (%s)' % (channel,msg.nick,ip,nick))
                              irc.reply('The ban on %s from %s has been lifted' % (nick,channel))
                          else:
                              self.logChannel(irc,'OP: [%s] %s asked for removal of %s (%s)' % (channel,msg.nick,ip,nick))
                              irc.reply(self.registryValue('msgInviteConfirm'))
               ops.append(channel)
       if len(ops):
           if not len(channels):
               irc.replyError("'%s' does not match any recent bans from %s" % (nick,', '.join(ops)))
       else:
           irc.replyError("Only **Opped** channel operators of the channel the ban originated in can remove k-lines. If you have any questions, contact freenode staff (#freenode-sigyn)")
    unkline = wrap(unkline,['private','text'])


    def oper (self,irc,msg,args):
        """takes no arguments

        ask bot to oper"""
        if len(self.registryValue('operatorNick')) and len(self.registryValue('operatorPassword')):
            irc.sendMsg(ircmsgs.IrcMsg('OPER %s %s' % (self.registryValue('operatorNick'),self.registryValue('operatorPassword'))))
            irc.replySuccess()
        else:
            irc.replyError('operatorNick or operatorPassword is empty')
    oper = wrap(oper,['owner'])

    def undline (self,irc,msg,args,txt):
        """<ip>
           undline an ip
        """
        irc.queueMsg(ircmsgs.IrcMsg('UNDLINE %s on *' % txt))
        irc.replySuccess()
    undline = wrap(undline,['owner','ip'])


    def checkresolve (self,irc,msg,args,txt):
        """<nick!ident@hostmask>

           returns computed hostmask"""
        irc.reply(self.prefixToMask(irc,txt))
    checkresolve = wrap(checkresolve,['owner','hostmask'])

    # internal stuff

    def applyDefcon (self, irc):
        i = self.getIrc(irc)
        for channel in irc.state.channels:
            if irc.isChannel(channel) and self.registryValue('defconMode',channel=channel):
                chan = self.getChan(irc,channel)
                if i.defcon or chan.called:
                    if not 'z' in irc.state.channels[channel].modes:
                        if irc.nick in list(irc.state.channels[channel].ops):
                            irc.sendMsg(ircmsgs.IrcMsg('MODE %s +qz $~a' % channel))
                        else:
                            irc.sendMsg(ircmsgs.IrcMsg('MODE %s +oqz %s $~a' % (channel,irc.nick)))



    def _ip_ranges (self, h):
        if '/' in h:
            # we've got a cloak
            parts = h.split('/')
            if parts[0] == 'gateway' and parts[-1].startswith('ip.'):
                # we've got a dehexed gateway IP cloak
                h = parts[-1].split('.', 1)[1]
            else:
                return [h]

        if utils.net.isIPV4(h):
            prefixes = [27, 26, 25, 24]
        elif utils.net.bruteIsIPV6(h):
            # noteworthy IPv6 allocation information
            # - linode assigns a /128 by default. can also offer /56, /64 & /116
            # - xfinity (comcast) has been reported as offering /60
            # - hurricane electric tunnel brokers get a /48

            prefixes = [120, 118, 116, 114, 112, 110, 64, 60, 56, 48]
        else:
            return [h]

        ranges = []
        for prefix in prefixes:
            range = ipaddress.ip_network('%s/%d' % (h, prefix), strict=False).with_prefixlen
            ranges.append(range)
        return ranges

    def resolve (self,irc,prefix,channel='',dnsbl=False,comment=False):
        (nick,ident,host) = ircutils.splitHostmask(prefix)
        if ident.startswith('~'):
            ident = '*'
        if prefix in self.cache:
            return self.cache[prefix]
        try:
            resolver = dns.resolver.Resolver()
            resolver.timeout = self.registryValue('resolverTimeout')
            resolver.lifetime = self.registryValue('resolverTimeout')
            L = []
            ips = None
            try:
                ips = resolver.query(host,'AAAA')
            except:
                ips = None
            if ips:
                for ip in ips:
                    if not str(ip) in L:
                        L.append(str(ip))
            try:
                ips = resolver.query(host,'A')
            except:
                ips = None
            if ips:
                for ip in ips:
                    if not str(ip) in L:
                        L.append(str(ip))
            #self.log.debug('%s resolved as %s' % (prefix,L))
            if len(L) == 1:
                h = L[0]
                #self.log.debug('%s is resolved as %s@%s' % (prefix,ident,h))
                if dnsbl:
                    if utils.net.isIPV4(h) or utils.net.bruteIsIPV6(h):
                        if len(self.registryValue('droneblKey')) and len(self.registryValue('droneblHost')) and self.registryValue('enable'):
                            t = world.SupyThread(target=self.fillDnsbl,name=format('fillDnsbl %s', h),args=(irc,h,self.registryValue('droneblHost'),self.registryValue('droneblKey'),comment))
                            t.setDaemon(True)
                            t.start()
                            if prefix in i.resolving:
                                del i.resolving[prefix]
                            return
                self.cache[prefix] = '%s@%s' % (ident,h)
            else:
                self.cache[prefix] = '%s@%s' % (ident,host)
        except:
            self.cache[prefix] = '%s@%s' % (ident,host)
        i = self.getIrc(irc)
        if channel and channel in irc.state.channels:
            chan = self.getChan(irc,channel)
            if nick in irc.state.channels[channel].users:
                if nick in chan.nicks:
                    chan.nicks[nick][2] = self.cache[prefix]
        if prefix in i.resolving:
            del i.resolving[prefix]

    def prefixToMask (self,irc,prefix,channel='',dnsbl=False,comment=None):
        if prefix in self.cache:
            return self.cache[prefix]
        prefix = prefix
        (nick,ident,host) = ircutils.splitHostmask(prefix)
        if '/' in host:
            if host.startswith('gateway/web/freenode'):
                if 'ip.' in host:
                    self.cache[prefix] = '*@%s' % host.split('ip.')[1]
                else:
                    # syn offline / busy
                    self.cache[prefix] = '%s@gateway/web/freenode/*' % ident
            elif host.startswith('gateway/tor-sasl'):
                self.cache[prefix] = '*@%s' % host
            elif host.startswith('gateway/vpn') or host.startswith('nat/'):
                if ident.startswith('~'):
                    ident = '*'
                if '/x-' in host:
                    host = host.split('/x-')[0] + '/*'
                self.cache[prefix] = '%s@%s' % (ident,host)
            elif host.startswith('gateway'):
                h = host.split('/')
                if 'ip.' in host:
                    ident = '*'
                    h = host.split('ip.')[1]
                elif '/vpn/' in host:
                    if '/x-' in host:
                        h = h[:3]
                        h = '%s/*' % '/'.join(h)
                    else:
                        h = host
                    if ident.startswith('~'):
                        ident = '*'
                elif len(h) > 3:
                    h = h[:3]
                    h = '%s/*' % '/'.join(h)
                else:
                    h = host
                self.cache[prefix] = '%s@%s' % (ident,h)
            else:
                if ident.startswith('~'):
                    ident = '*'
                self.cache[prefix] = '%s@%s' % (ident,host)
        else:
            if ident.startswith('~'):
                ident = '*'
            if utils.net.isIPV4(host):
                self.cache[prefix] = '%s@%s' % (ident,host)
            elif utils.net.bruteIsIPV6(host):
                self.cache[prefix] = '%s@%s' % (ident,host)
            else:
                i = self.getIrc(irc)
                if self.registryValue('useWhoWas'):
                    self.cache[prefix] = '%s@%s' % (ident,host)
                elif not prefix in i.resolving:
                    i.resolving[prefix] = True
                    t = world.SupyThread(target=self.resolve,name=format('resolve %s', prefix),args=(irc,prefix,channel,dnsbl,comment))
                    t.setDaemon(True)
                    t.start()
                    return '%s@%s' % (ident,host)
        if prefix in self.cache:
            return self.cache[prefix]
        else:
            if ident.startswith('~'):
                ident = '*'
            return '%s@%s' % (ident,host)

    def do352 (self,irc,msg):
        # RPL_WHOREPLY
        channel = msg.args[1]
        (nick, ident, host) = (msg.args[5], msg.args[2], msg.args[3])
        if irc.isChannel(channel):
            chan = self.getChan(irc,channel)
            t = time.time()
            prefix = '%s!%s@%s' % (nick,ident,host)
            mask = self.prefixToMask(irc,prefix,channel)
            if isCloaked(prefix,self):
                t = t - self.registryValue('ignoreDuration',channel=channel) - 1
            chan.nicks[nick] = [t,prefix,mask,'','']

    def spam (self,irc,msg,args,channel):
        """<channel>

        trusted users can ask the bot to join <channel> for a limited period of time
        """
        if not channel in irc.state.channels:
            t = time.time() - (self.registryValue('leaveChannelIfNoActivity',channel=channel) * 24 * 3600) + 3600
            self.setRegistryValue('lastActionTaken',t,channel=channel)
            irc.sendMsg(ircmsgs.join(channel))
            chan = self.getChan(irc,channel)
            chan.requestedBySpam = True
            self.logChannel(irc,"JOIN: [%s] due to %s (trusted)" % (channel,msg.prefix))
            try:
                network = conf.supybot.networks.get(irc.network)
                network.channels().add(channel)
            except KeyError:
                pass
            irc.replySuccess()
    spam = wrap(spam,[('checkCapability','trusted'),'channel'])

    def unstaffed (self,irc,msg,args):
        """

        returns monitored channels without staffers
        """
        channels = []
        for channel in irc.state.channels:
            found = False
            for nick in list(irc.state.channels[channel].users):
                try:
                    hostmask = irc.state.nickToHostmask(nick)
                    if ircutils.isUserHostmask(hostmask) and self.registryValue('staffCloak') in hostmask:
                        found = True
                        break
                except:
                    continue
            if not found:
                channels.append(channel)
        irc.reply('%s channels: %s' %(len(channels),', '.join(channels)))
    unstaffed = wrap(unstaffed,['owner'])

    def list (self,irc,msg,args):
       """

       returns list of monitored channels with their users count and * if leaveChannelIfNoActivity is -1
       """
       channels = []
       for channel in list(irc.state.channels):
           flag = ''
           if self.registryValue('leaveChannelIfNoActivity',channel=channel) == -1:
               flag = '*'
           l = len(irc.state.channels[channel].users)
           if not channel == self.registryValue('secretChannel') and not channel == self.registryValue('snoopChannel') and not channel == self.registryValue('reportChannel') and not channel == self.registryValue('logChannel'):
               channels.append((l,flag,channel))
       def getKey(item):
           return item[0]
       chs = sorted(channels,key=getKey,reverse=True)
       channels = []
       for c in chs:
           (l,flag,channel) = c
           channels.append('%s %s(%s)' % (channel,flag,l))
       irc.reply('%s channels: %s' %(len(channels),', '.join(channels)))
    list = wrap(list,['owner'])

    def do001 (self,irc,msg):
        i = self.getIrc(irc)
        if not i.opered:
            if len(self.registryValue('operatorNick')) and len(self.registryValue('operatorPassword')):
                irc.queueMsg(ircmsgs.IrcMsg('OPER %s %s' % (self.registryValue('operatorNick'),self.registryValue('operatorPassword'))))

    def do381 (self,irc,msg):
        i = self.getIrc(irc)
        if not i.opered:
            i.opered = True
            irc.queueMsg(ircmsgs.IrcMsg('MODE %s +p' % irc.nick))
            irc.queueMsg(ircmsgs.IrcMsg('MODE %s +s +Fbnfl' % irc.nick))
            try:
                conf.supybot.protocols.irc.throttleTime.setValue(0.0)
            except:
                t = True

    def doMode (self,irc,msg):
        target = msg.args[0]
        if target == irc.nick:
            i = self.getIrc(irc)
            modes = ircutils.separateModes(msg.args[1:])
            for change in modes:
                (mode,value) = change
                if mode == '-o':
                    i.opered = False
                    if len(self.registryValue('operatorNick')) and len(self.registryValue('operatorPassword')):
                        irc.queueMsg(ircmsgs.IrcMsg('OPER %s %s' % (self.registryValue('operatorNick'),self.registryValue('operatorPassword'))))
                elif mode == '+p':
                    i.god = True
                    self.log.debug('%s is switching to god' % irc.nick)
                    self.applyDefcon(irc)
                elif mode == '-p':
                    i.god = False
                    self.log.debug('%s is switching to mortal' % irc.nick)
        elif target in irc.state.channels and 'm' in irc.state.channels[target].modes:
            modes = ircutils.separateModes(msg.args[1:])
            for change in modes:
                (mode,value) = change
                if mode == '+v':
                    chan = self.getChan(irc,target)
                    if value in chan.nicks:
                        a = chan.nicks[value]
                        if len(a) == 5:
                            chan.nicks[msg.nick] = [time.time(),a[1],a[2],a[3],a[4]]
                        else:
                            chan.nicks[msg.nick] = [time.time(),a[1],a[2],'','']
        elif target in irc.state.channels:
            modes = ircutils.separateModes(msg.args[1:])
            for change in modes:
                (mode,value) = change
                if mode == '+z':
                    if not irc.nick in list(irc.state.channels[target].ops):
                        irc.queueMsg(ircmsgs.IrcMsg('PRIVMSG ChanServ :OP %s' % target))
                    if target == self.registryValue('mainChannel'):
                        self.opStaffers(irc)
                elif mode == '+b' or mode == '+q':
                    if ircutils.isUserHostmask(value):
                        mask = self.prefixToMask(irc,value)
                        ip = mask.split('@')[1]
                        permit = self.registryValue('banPermit')
                        if permit > -1:
                            ipranges = self._ip_ranges(ip)
                            announced = False
                            for range in ipranges:
                                range = range
                                q = self.getIrcQueueFor(irc,'ban-check',range,self.registryValue('banLife'))
                                q.enqueue(target)
                                if len(q) > permit:
                                    chs = []
                                    for m in q:
                                        chs.append(m)
                                    q.reset()
                                    if not announced:
                                        announced = True
                                        self.logChannel(irc,"INFO: *@%s is collecting bans (%s/%ss) %s" % (range, permit, self.registryValue('banLife'), ','.join(chs)))
                                permit = permit + 1

    def opStaffers (self,irc):
        ops = []
        if self.registryValue('mainChannel') in irc.state.channels and irc.nick in list(irc.state.channels[self.registryValue('mainChannel')].ops):
           for nick in list(irc.state.channels[self.registryValue('mainChannel')].users):
               if not nick in list(irc.state.channels[self.registryValue('mainChannel')].ops):
                   try:
                       mask = irc.state.nickToHostmask(nick)
                       if mask and self.registryValue('staffCloak') in mask:
                           ops.append(nick)
                   except:
                       continue
        if len(ops):
            for i in range(0, len(ops), 4):
                irc.sendMsg(ircmsgs.ops(self.registryValue('mainChannel'),ops[i:i+4],irc.prefix))

    def getIrc (self,irc):
        if not irc.network in self._ircs:
            self._ircs[irc.network] = Ircd(irc)
            self._ircs[irc.network].restore(self.getDb(irc.network))
            if len(self.registryValue('operatorNick')) and len(self.registryValue('operatorPassword')):
                irc.queueMsg(ircmsgs.IrcMsg('OPER %s %s' % (self.registryValue('operatorNick'),self.registryValue('operatorPassword'))))
        return self._ircs[irc.network]

    def doAccount (self,irc,msg):
        i = self.getIrc(irc)
        if ircutils.isUserHostmask(msg.prefix):
            nick = ircutils.nickFromHostmask(msg.prefix)
            acc = msg.args[0]
            if acc == '*':
                acc = None
            else:
                aa = acc.lower()
                for u in i.klinednicks:
                    if aa == u:
                        self.logChannel(irc,"SERVICE: %s (%s) lethal account (account-notify)" % (msg.prefix,acc))
                        src = msg.nick
                        i.klinednicks.enqueue(aa)
                        if not src in i.tokline:
                            i.toklineresults[src] = {}
                            i.toklineresults[src]['kind'] = 'evade'
                            i.tokline[src] = src
                            def f ():
                                irc.sendMsg(ircmsgs.IrcMsg('WHOIS %s %s' % (src,src)))
                            schedule.addEvent(f,time.time()+random.randint(0,7))
                            #irc.sendMsg(ircmsgs.IrcMsg('WHOIS %s %s' % (src,src)))
                        break
            for channel in irc.state.channels:
                if irc.isChannel(channel):
                    chan = self.getChan(irc,channel)
                    if nick in chan.nicks:
                        a = chan.nicks[msg.nick]
                        if len(a) == 5:
                            chan.nicks[msg.nick] = [a[0],a[1],a[2],a[3],acc]
                        else:
                            chan.nicks[msg.nick] = [a[0],a[1],a[2],'',acc]

    def getChan (self,irc,channel):
        i = self.getIrc(irc)
        if not channel in i.channels and irc.isChannel(channel):
            i.channels[channel] = Chan(channel)
            if not self.starting:
                irc.queueMsg(ircmsgs.who(channel))
        return i.channels[channel]

    def kill (self,irc,nick,reason=None):
        i = self.getIrc(irc)
        if i.defcon:
            i.defcon = time.time()
        if not self.registryValue('enable'):
            self.logChannel(irc,"INFO: disabled, can't kill %s" % nick)
            return
        if not i.opered:
            self.logChannel(irc,"INFO: not opered, can't kill %s" % nick)
            return
        if not reason:
            reason = self.registryValue('killMessage')
        irc.sendMsg(ircmsgs.IrcMsg('KILL %s :%s' % (nick,reason)))

    def do338 (self,irc,msg):
        i = self.getIrc(irc)
        if msg.args[0] == irc.nick and msg.args[1] in i.whowas:
            pending = i.whowas[msg.args[1]]
            del i.whowas[msg.args[1]]
            (nick,ident,host) = ircutils.splitHostmask(pending[0])
            # [prefix,mask,duration,reason,klineMessage]
            ident = pending[1].split('@')[0]
            h = msg.args[2]
            if h == '255.255.255.255':
               h = host
            mask = self.prefixToMask(irc,'%s!%s@%s' % (nick,ident,h))
            if not self.registryValue('enable'):
                self.logChannel(irc,"INFO: disabled, can't kline %s (%s)" % (mask,pending[3]))
                if pending[1] in i.klines:
                    del i.klines[pending[1]]
                return
            if not i.opered:
                self.logChannel(irc,"INFO: not opered, can't kline %s (%s)" % (mask,pending[3]))
                if pending[1] in i.klines:
                    del i.klines[pending[1]]
                return
            self.log.info('KLINE %s|%s' % (mask,pending[3]))
            if self.registryValue('useOperServ'):
                irc.sendMsg(ircmsgs.IrcMsg('PRIVMSG OperServ :AKILL ADD %s !T %s %s | %s' % (mask,pending[2],pending[4],pending[3])))
            else:
                irc.sendMsg(ircmsgs.IrcMsg('KLINE %s %s :%s|%s' % (pending[2],mask,pending[4],pending[3])))
            nickLowered = nick.lower()
            for channel in irc.state.channels:
                chan = self.getChan(irc,channel)
                if len(chan.klines):
                    index = 0
                    for k in chan.klines:
                       if k.startswith(nickLowered):
                           (at, m) = chan.klines.queue[index]
                           chan.klines.queue[index] = (at,'%s %s' % (nickLowered,mask))
                           self.log.info('kline %s replaced at %s: %s / %s' % (m,index,nickLowered,mask))
                           break
                       index = index + 1
            if pending[1] in i.klines:
                del i.klines[pending[1]]

    def kline (self,irc,prefix,mask,duration,reason,klineMessage=None):
        i = self.getIrc(irc)
        if mask in i.klines:
            return
        if duration < 0:
            self.log.info('Ignored kline %s due to no duration', mask)
            return
        if not klineMessage:
            klineMessage = self.registryValue('klineMessage')
        if '"' in klineMessage:
            klineMessage = self.registryValue('klineMessage')
        canKline = True
        i.klines[mask] = mask
        if "bc.googleusercontent.com" in prefix:
            reason = reason + ' !dnsbl Unknown spambot or drone'
        if ircutils.isUserHostmask(prefix):
           canKline = not self.registryValue('useWhoWas')
           if i.defcon or 'gateway/' in prefix:
               canKline = True
        else:
            self.log.info('INVALID PREFIX %s : %s : %s' % (prefix,mask,reason))
        self.log.info('CANKLINE %s %s %s' % (prefix,mask,canKline))
        if canKline:
            if not self.registryValue('enable'):
                self.logChannel(irc,"INFO: disabled, can't kline %s (%s)" % (mask,reason))
            else:
                self.log.info('KLINE %s|%s' % (mask,reason))
                if self.registryValue('useOperServ'):
                    irc.sendMsg(ircmsgs.IrcMsg('PRIVMSG OperServ :AKILL ADD %s !T %s %s | %s' % (mask,duration,klineMessage,reason)))
                else:
                    irc.sendMsg(ircmsgs.IrcMsg('KLINE %s %s :%s|%s' % (duration,mask,klineMessage,reason)))
                if i.defcon:
                    i.defcon = time.time()
        elif ircutils.isUserHostmask(prefix):
            (nick,ident,host) = ircutils.splitHostmask(prefix)
            self.log.info('whowas for %s | %s | %s' % (prefix,mask,reason))
            if not nick in i.whowas:
                i.whowas[nick] = [prefix,mask,duration,reason,klineMessage]
                irc.sendMsg(ircmsgs.IrcMsg('WHOWAS %s' % nick))
        def forgetKline ():
            i = self.getIrc(irc)
            if mask in i.klines:
                del i.klines[mask]
        schedule.addEvent(forgetKline,time.time()+7)

    def ban (self,irc,nick,prefix,mask,duration,reason,message,log,killReason=None):
        self.kill(irc,nick,killReason)
        self.kline(irc,prefix,mask,duration,reason,message)
        self.logChannel(irc,log)

    def getIrcQueueFor (self,irc,key,kind,life):
        i = self.getIrc(irc)
        if not key in i.queues:
            i.queues[key] = {}
        if not kind in i.queues[key]:
            i.queues[key][kind] = utils.structures.TimeoutQueue(life)
        elif i.queues[key][kind].timeout != life:
            i.queues[key][kind].setTimeout(life)
        return i.queues[key][kind]

    def rmIrcQueueFor (self,irc,key):
        i = self.getIrc(irc)
        if key in i.queues:
            for k in i.queues[key]:
                if type(i.queues[key][k]) == utils.structures.TimeoutQueue:
                    i.queues[key][k].reset()
                    i.queues[key][k].queue = None
            i.queues[key].clear()
            del i.queues[key]

    def do015 (self,irc,msg):
        try:
            (targets,text) = msg.args
            i = self.getIrc(irc)
            reg = r".*-\s+([a-z]+\.freenode\.net)\[.*Users:\s+(\d{2,6})\s+"
            result = re.match(reg,text)
            # here we store server name and users count, and we will ping the server with the most users
            if result:
                i.servers[result.group(1)] = int(result.group(2))
        except:
            pass

    def do017 (self,irc,msg):
        found = None
        users = None
        i = self.getIrc(irc)
        for server in i.servers:
            if not users or users < i.servers[server]:
                found = server
                users = i.servers[server]
        server = None
        if found:
            i.servers = {}
            server = '%s' % found
            i.servers[server] = time.time()
            def bye():
                i = self.getIrc(irc)
                if server in i.servers:
                    del i.servers[server]
                    if not i.netsplit:
                        self.logChannel(irc,'INFO: netsplit activated for %ss due to %s/%ss of lags with %s : some abuses are ignored' % (self.registryValue('netsplitDuration'),self.registryValue('lagPermit'),self.registryValue('lagPermit'),server))
                    i.netsplit = time.time() + self.registryValue('netsplitDuration')
            schedule.addEvent(bye,time.time()+self.registryValue('lagPermit'))
            irc.queueMsg(ircmsgs.IrcMsg('TIME %s' % server))

    def resync (self,irc,msg,args):
        """in case of plugin being reloaded
           call this to recompute user to ignore (ignoreDuration)"""
        for channel in irc.state.channels:
            irc.queueMsg(ircmsgs.who(channel))
        irc.replySuccess()
    resync = wrap(resync,['owner'])

    def lethalaccount (self,irc,msg,args,text):
        """<accountname> monitor account and kline it on sight
           during 24h, via extended-join, account-notify, account's name change"""
        i = self.getIrc(irc)
        account = text.lower().strip()
        i.klinednicks.enqueue(account)
        self.logChannel(irc,'SERVICE: %s lethaled for 24h by %s' % (account, msg.nick))
        for channel in irc.state.channels:
            if irc.isChannel(channel):
               c = self.getChan(irc,channel)
               for u in list(irc.state.channels[channel].users):
                  if u in c.nicks:
                      if len(c.nicks[u]) > 4:
                          if c.nicks[u][4] and c.nicks[u][4].lower() == account:
                              self.ban(irc,u,c.nicks[u][1],c.nicks[u][2],self.registryValue('klineDuration'),'Lethaled account %s' % account,self.registryValue('klineMessage'),'BAD: %s (lethaled account %s)' % (account,c.nicks[u][1]),self.registryValue('killMessage'))
        irc.replySuccess()
    lethalaccount = wrap(lethalaccount,['owner','text'])

    def cleanup (self,irc):
        i = self.getIrc(irc)
        partReason = 'Leaving the channel. /invite %s %s again if needed'
        for channel in irc.state.channels:
            if irc.isChannel(channel) and not channel in self.registryValue('mainChannel') and not channel == self.registryValue('snoopChannel') and not channel == self.registryValue('logChannel') and not channel == self.registryValue('reportChannel') and not channel == self.registryValue('secretChannel'):
                if self.registryValue('lastActionTaken',channel=channel) > 1.0 and self.registryValue('leaveChannelIfNoActivity',channel=channel) > -1 and not i.defcon:
                    if time.time() - self.registryValue('lastActionTaken',channel=channel) > (self.registryValue('leaveChannelIfNoActivity',channel=channel) * 24 * 3600):
                       irc.queueMsg(ircmsgs.part(channel, partReason % (irc.nick,channel)))
                       chan = self.getChan(irc,channel)
                       if chan.requestedBySpam:
                           self.setRegistryValue('lastActionTaken',self.registryValue('lastActionTaken'),channel=channel)
                       else:
                           self.setRegistryValue('lastActionTaken',time.time(),channel=channel)
                       self.logChannel(irc,'PART: [%s] due to inactivity for %s days' % (channel,self.registryValue('leaveChannelIfNoActivity',channel=channel)))
                       try:
                           network = conf.supybot.networks.get(irc.network)
                           network.channels().remove(channel)
                       except KeyError:
                           pass
        kinds = []
        for kind in i.queues:
            count = 0
            ks = []
            try:
                for k in i.queues[kind]:
                    if isinstance(i.queues[kind][k],utils.structures.TimeoutQueue):
                        if not len(i.queues[kind][k]):
                            ks.append(k)
                        else:
                           count += 1
                    else:
                        count += 1
            except:
                self.log.error('Exception with %s' % kind)
            if len(ks):
                for k in ks:
                    del i.queues[kind][k]
            if count == 0:
               kinds.append(kind)
        for kind in kinds:
            del i.queues[kind]
        chs = []
        for channel in i.channels:
            chan = i.channels[channel]
            ns = []
            for n in chan.nicks:
                if channel in irc.state.channels:
                  if not n in irc.state.channels[channel].users:
                        ns.append(n)
                else:
                    ns.append(n)
            for n in ns:
                del chan.nicks[n]
            bs = []
            for b in chan.buffers:
                 qs = []
                 count = 0
                 for q in chan.buffers[b]:
                     if isinstance(chan.buffers[b][q],utils.structures.TimeoutQueue):
                         if not len(chan.buffers[b][q]):
                            qs.append(q)
                         else:
                            count += 1
                     else:
                        count +=1
                 for q in qs:
                     del chan.buffers[b][q]
                 if count == 0:
                     bs.append(b)
            for b in bs:
                del chan.buffers[b]
            logs = []
            if chan.logs:
                for log in chan.logs:
                    if not len(chan.logs[log]):
                        logs.append(log)
            for log in logs:
                del chan.logs[log]
            if len(ns) or len(bs) or len(logs):
                chs.append('[%s : %s nicks, %s buffers, %s logs]' % (channel,len(ns),len(bs),len(logs)))

    def do391 (self,irc,msg):
        i = self.getIrc(irc)
        if msg.prefix in i.servers:
            delay = time.time()-i.servers[msg.prefix]
            del i.servers[msg.prefix]
            if delay > self.registryValue('lagPermit'):
                if not i.netsplit:
                    self.logChannel(irc,'INFO: netsplit activated for %ss due to %s/%ss of lags with %s : some abuses are ignored' % (self.registryValue('netsplitDuration'),delay,self.registryValue('lagPermit'),msg.prefix))
                i.netsplit = time.time() + self.registryValue('netsplitDuration')

    def do219 (self,irc,msg):
        i = self.getIrc(irc)
        r = []
        for k in i.stats:
            if i.stats[k] > self.registryValue('ghostPermit'):
                r.append(k.replace('[unknown@','').replace(']',''))
        for ip in r:
            irc.sendMsg(ircmsgs.IrcMsg('DLINE %s %s on * :%s' % (1440,ip,self.registryValue('msgTooManyGhost'))))
        i.stats = {}
        if len(r):
            self.logChannel(irc,'DOS: %s ip(s) %s' % (len(r),', '.join(r)))
        if len(i.dlines):
            for l in i.dlines:
                found = False
                for ip in i.ilines:
                    if l in ip:
                        found = True
                        break
                if not found:
                    self.log.info('DLINE %s|%s' % (l,self.registryValue('saslDuration')))
                    irc.sendMsg(ircmsgs.IrcMsg('DLINE %s %s on * :%s' % (self.registryValue('saslDuration'),l,self.registryValue('saslMessage'))))
            i.dlines = []
            i.ilines = {}

    def do311 (self,irc,msg):
       i = self.getIrc(irc)
       nick = msg.args[1]
       if nick in i.mx:
           ident = msg.args[2]
           hostmask = '%s!%s@%s' % (nick,ident,msg.args[3])
           email = i.mx[nick][0]
           badmail = i.mx[nick][1]
           mx = i.mx[nick][2]
           freeze = i.mx[nick][3]
           del i.mx[nick]
           mask = self.prefixToMask(irc,hostmask)
           self.logChannel(irc,'SERVICE: %s registered %s with *@%s is in mxbl (%s)' % (hostmask,nick,email,mx))
           if badmail and len(email) and len(nick):
               if not freeze:
                   irc.queueMsg(ircmsgs.notice(nick,'Your account has been dropped, please register it again with a valid email address (no disposable temporary email)'))
       elif nick in i.tokline:
          if not nick in i.toklineresults:
              i.toklineresults[nick] = {}
          ident = msg.args[2]
          hostmask = '%s!%s@%s' % (nick,ident,msg.args[3])
          mask = self.prefixToMask(irc,hostmask)
          gecos = msg.args[5]
          i.toklineresults[nick]['hostmask'] = hostmask
          i.toklineresults[nick]['mask'] = mask
          i.toklineresults[nick]['gecos'] = gecos

    def do317 (self,irc,msg):
       i = self.getIrc(irc)
       nick = msg.args[1]
       if nick in i.tokline:
           if not nick in i.toklineresults:
               i.toklineresults[nick] = {}
           i.toklineresults[nick]['signon'] = float(msg.args[3])

    def do330 (self,irc,msg):
       i = self.getIrc(irc)
       nick = msg.args[1]
       if nick in i.tokline:
           if not nick in i.toklineresults:
               i.toklineresults[nick] = {}
           i.toklineresults[nick]['account'] = True

    def do318 (self,irc,msg):
       i = self.getIrc(irc)
       nick = msg.args[1]
       if nick in i.toklineresults:
           if i.toklineresults[nick]['kind'] == 'evade':
               uid = random.randint(0,1000000)
               irc.sendMsg(ircmsgs.IrcMsg('KLINE %s %s :%s|%s' % (self.registryValue('klineDuration'),i.toklineresults[nick]['mask'],self.registryValue('klineMessage'),'%s - kline evasion' % (uid))))
               self.logChannel(irc,'BAD: [%s] %s (kline evasion)' % (i.toklineresults[nick]['hostmask'],uid))
           del i.tokline[nick]
           del i.toklineresults[nick]

    def doInvite(self, irc, msg):
       channel = msg.args[1]
       i = self.getIrc(irc)
       self.log.info('%s inviting %s in %s (%s | %s | %s)' % (msg.prefix,irc.nick,channel,self.registryValue('leaveChannelIfNoActivity',channel=channel),self.registryValue('lastActionTaken',channel=channel),self.registryValue('minimumUsersInChannel')))
       if channel and not channel in irc.state.channels and not ircdb.checkIgnored(msg.prefix):
           if self.registryValue('leaveChannelIfNoActivity',channel=channel) == -1:
               irc.queueMsg(ircmsgs.join(channel))
               self.logChannel(irc,"JOIN: [%s] due to %s's invite" % (channel,msg.prefix))
               try:
                   network = conf.supybot.networks.get(irc.network)
                   network.channels().add(channel)
               except KeyError:
                   pass
           elif self.registryValue('lastActionTaken',channel=channel) > 0.0:
               if self.registryValue('minimumUsersInChannel') > -1:
                   i.invites[channel] = msg.prefix
                   irc.queueMsg(ircmsgs.IrcMsg('LIST %s' % channel))
               else:
                   self.setRegistryValue('lastActionTaken',time.time(),channel=channel)
                   irc.queueMsg(ircmsgs.join(channel))
                   self.logChannel(irc,"JOIN: [%s] due to %s's invite" % (channel,msg.prefix))
                   try:
                       network = conf.supybot.networks.get(irc.network)
                       network.channels().add(channel)
                   except KeyError:
                       pass
                   irc.queueMsg(ircmsgs.privmsg(channel,'** Warning: if there is any bot in %s which should be exempted from %s, contact staffers before it gets caught **' % (channel,irc.nick)))
           else:
               self.logChannel(irc,'INVITE: [%s] %s is asking for %s' % (channel,msg.prefix,irc.nick))
               irc.queueMsg(ircmsgs.privmsg(msg.nick,'The invitation to %s will be reviewed by staff' % channel))

    def do322 (self,irc,msg):
        i = self.getIrc(irc)
        if msg.args[1] in i.invites:
            if int(msg.args[2]) > self.registryValue('minimumUsersInChannel'):
                self.setRegistryValue('lastActionTaken',time.time(),channel=msg.args[1])
                irc.queueMsg(ircmsgs.join(msg.args[1]))
                try:
                    network = conf.supybot.networks.get(irc.network)
                    network.channels().add(msg.args[1])
                except KeyError:
                    pass
                self.logChannel(irc,"JOIN: [%s] due to %s's invite (%s users)" % (msg.args[1],i.invites[msg.args[1]],msg.args[2]))
                irc.queueMsg(ircmsgs.privmsg(msg.args[1],'** Warning: if there is any bot in %s which should be exempted from %s, contact staffers before it gets caught **' % (msg.args[1],irc.nick)))
            else:
                self.logChannel(irc,"INVITE: [%s] by %s denied (%s users)" % (msg.args[1],i.invites[msg.args[1]],msg.args[2]))
                (nick,ident,host) = ircutils.splitHostmask(i.invites[msg.args[1]])
                irc.queueMsg(ircmsgs.privmsg(nick,'Invitation denied, there are only %s users in %s (%s minimum for %s): contact staffers if needed.' % (msg.args[2],msg.args[1],self.registryValue('minimumUsersInChannel'),irc.nick)))
            del i.invites[msg.args[1]]

    def resolveSnoopy (self,irc,account,email,badmail,freeze):
       resolver = dns.resolver.Resolver()
       resolver.timeout = 10
       resolver.lifetime = 10
       found = ''
       items = self.registryValue('mxbl')
       for item in items:
           if email in item:
               found = item
               break
       i = self.getIrc(irc)
       if email in i.domains:
           found = email
       ips = None
       if not len(found):
           try:
               ips = resolver.query(email,'MX')
           except:
               ips = None
       if ips:
           for ip in ips:
               ip = '%s' % ip
               ip = ip.split(' ')[1][:-1]
               for item in items:
                   if ip in item:
                       found = item
                       break
               if len(found):
                   break
               q = None
               try:
                   q = resolver.query(ip,'A')
               except:
                   q = None
               if q:
                   for i in q:
                       i = '%s' % i
                       for item in items:
                           if i in item:
                               found = ip
                               break
                       if len(found):
                           break
               if len(found):
                   break
       i = self.getIrc(irc)
       if len(found):
           i.mx[account] = [email,badmail,found,freeze]
           if badmail and len(email):
               irc.queueMsg(ircmsgs.IrcMsg('PRIVMSG NickServ :BADMAIL ADD *@%s %s' % (email,found)))
               if not freeze:
                   irc.queueMsg(ircmsgs.IrcMsg('PRIVMSG NickServ :FDROP %s' % account))
               else:
                   irc.queueMsg(ircmsgs.IrcMsg('PRIVMSG NickServ :FREEZE %s ON changed email to (%s which is in mxbl %s)' % (account,email,found)))
           irc.queueMsg(ircmsgs.IrcMsg('WHOIS %s' % account))
       else:
           i.cleandomains[email] = True

    def handleSnoopMessage (self,irc,msg):
        (targets, text) = msg.args
        text = text.replace('\x02','')
        if msg.nick == 'NickServ' and 'REGISTER:' in text:
            email = text.split('@')[1]
            account = text.split(' ')[0]
            i = self.getIrc(irc)
            if not email in i.cleandomains:
                t = world.SupyThread(target=self.resolveSnoopy,name=format('Snoopy %s', email),args=(irc,account,email,True,False))
                t.setDaemon(True)
                t.start()
            account = account.lower().strip()
            q = self.getIrcQueueFor(irc,account,'nsregister',600)
            q.enqueue(email)
        if msg.nick == 'NickServ':
            src = text.split(' ')[0].lower().strip()
            target = ''
            registering = True
            grouping = False
            if ' GROUP:' in text:
                grouping = True
                target = text.split('(')[1].split(')')[0]
            elif 'SET:ACCOUNTNAME:' in text:
                grouping = True
                t = text.split('(')
                if len(t) > 1:
                    target = text.split('(')[1].split(')')[0]
                else:
                    return
            elif 'UNGROUP: ' in text:
                grouping = True
                target = text.split('UNGROUP: ')[1]
            if len(target) and grouping:
                q = self.getIrcQueueFor(irc,src,'nsAccountGroup',120)
                q.enqueue(text)
                if len(q) == 3:
                    index = 0
                    a = b = c = False
                    oldAccount = None
                    for m in q:
                       if ' GROUP:' in m and index == 0:
                           a = True
                       elif ' SET:ACCOUNTNAME:' in m and index == 1:
                           oldAccount = m.split(' ')[1].replace('(','').replace('(','')
                           b = True
                       elif ' UNGROUP:' in m and index == 2:
                           c = True
                       index = index + 1
                    q.reset()
                    if a and b and c:
                        self.logChannel(irc,"SERVICE: %s suspicious evades/abuses with GROUP/ACCOUNTNAME/UNGROUP (was %s)" % (src,oldAccount))
                        i = self.getIrc(irc)
                        oldAccount = oldAccount.lower().strip()
                        for u in i.klinednicks:
                            if u == oldAccount:
                                self.logChannel(irc,"SERVICE: %s lethaled (%s), enforcing" % (src,oldAccount))
                                i.klinednicks.enqueue(src)
                                if not src in i.tokline:
                                    i.toklineresults[src] = {}
                                    i.toklineresults[src]['kind'] = 'evade'
                                    i.tokline[src] = src
                                    def f ():
                                        irc.sendMsg(ircmsgs.IrcMsg('WHOIS %s %s' % (src,src)))
                                    schedule.addEvent(f,time.time()+random.randint(0,7))
                                break
    def do211 (self,irc,msg):
        i = self.getIrc(irc)
        if msg.args[1].startswith('[unknown@'):
            if msg.args[1] in i.stats:
                i.stats[msg.args[1]] = i.stats[msg.args[1]] + 1
            else:
                i.stats[msg.args[1]] = 0

    def do728 (self,irc,msg):
        i = self.getIrc(irc)
        channel = msg.args[1]
        value = msg.args[3]
        op = msg.args[4]
        if self.registryValue('defconMode',channel=channel) and not i.defcon:
            if value == '$~a' and op == irc.prefix:
                if channel == self.registryValue('mainChannel'):
                    irc.sendMsg(ircmsgs.IrcMsg('MODE %s -qz $~a' % channel))
                else:
                    irc.sendMsg(ircmsgs.IrcMsg('MODE %s -qzo $~a %s' % (channel,irc.nick)))

    def handleMsg (self,irc,msg,isNotice):
        if not ircutils.isUserHostmask(msg.prefix):
            return
        if msg.prefix == irc.prefix:
            return
        (targets, t) = msg.args
        if ircmsgs.isAction(msg):
            text = ircmsgs.unAction(msg)
        else:
            text = t
        try:
            raw = ircutils.stripFormatting(text)
        except:
            raw = text
        text = raw.lower()
        mask = self.prefixToMask(irc,msg.prefix)
        i = self.getIrc(irc)
        if not i.ping or time.time() - i.ping > self.registryValue('lagInterval'):
            i.ping = time.time()
            self.cleanup(irc)
            if self.registryValue('lagPermit') > -1:
                i.stats = {}
                if self.registryValue('ghostPermit') > -1:
                    irc.queueMsg(ircmsgs.IrcMsg('STATS L'))
                irc.queueMsg(ircmsgs.IrcMsg('MAP'))
        if i.defcon:
            if time.time() > i.defcon + self.registryValue('defcon'):
                i.lastDefcon = time.time()
                i.defcon = False
                self.logChannel(irc,"INFO: triggers restored to normal behaviour")
                for channel in irc.state.channels:
                    if irc.isChannel(channel) and self.registryValue('defconMode',channel=channel):
                        if 'z' in irc.state.channels[channel].modes and irc.nick in list(irc.state.channels[channel].ops) and not 'm' in irc.state.channels[channel].modes:
                            irc.queueMsg(ircmsgs.IrcMsg('MODE %s q' % channel))
        if i.netsplit:
            if time.time() > i.netsplit:
                i.netsplit = False
                self.logChannel(irc,"INFO: netsplit mode desactivated")
        if mask in i.klines:
            self.log.debug('Ignoring %s (%s) - kline in progress', msg.prefix,mask)
            return
        isBanned = False
        for channel in targets.split(','):
            if channel.startswith('@'):
                channel = channel.replace('@','',1)
            if channel.startswith('+'):
                channel = channel.replace('+','',1)
            if irc.isChannel(channel) and channel in irc.state.channels:
                if self.registryValue('reportChannel') == channel:
                    self.handleReportMessage(irc,msg)
                if self.registryValue('snoopChannel') == channel:
                    self.handleSnoopMessage(irc,msg)
                if self.registryValue('secretChannel') == channel:
                    self.handleSecretMessage(irc,msg)
                if self.registryValue('ignoreChannel',channel):
                    continue
                if ircdb.checkCapability(msg.prefix, 'protected'):
                    if msg.nick in list(irc.state.channels[channel].ops) and irc.nick in text:
                        self.logChannel(irc,'OP: [%s] <%s> %s' % (channel,msg.nick,text))
                    continue
                chan = self.getChan(irc,channel)
                if chan.called:
                    if time.time() - chan.called > self.registryValue('abuseDuration',channel=channel):
                        chan.called = False
                        if not i.defcon:
                            self.logChannel(irc,'INFO: [%s] returns to regular state' % channel)
                        if irc.isChannel(channel) and self.registryValue('defconMode',channel=channel) and not i.defcon:
                            if 'z' in irc.state.channels[channel].modes and irc.nick in list(irc.state.channels[channel].ops) and not 'm' in irc.state.channels[channel].modes:
                                irc.queueMsg(ircmsgs.IrcMsg('MODE %s q' % channel))
                if isBanned:
                    continue
                if msg.nick in list(irc.state.channels[channel].ops):
                    if irc.nick in raw:
                        self.logChannel(irc,'OP: [%s] <%s> %s' % (channel,msg.nick,text))
                    continue
                if self.registryValue('ignoreVoicedUser',channel=channel):
                    if msg.nick in list(irc.state.channels[channel].voices):
                        continue
                protected = ircdb.makeChannelCapability(channel, 'protected')
                if ircdb.checkCapability(msg.prefix, protected):
                    continue
                if self.registryValue('ignoreRegisteredUser',channel=channel):
                    if msg.nick in chan.nicks and len(chan.nicks[msg.nick]) > 4:
                        if chan.nicks[msg.nick][4]:
                            continue
                killReason = self.registryValue('killMessage',channel=channel)
                if msg.nick in chan.nicks and len(chan.nicks[msg.nick]) > 4:
                    if chan.nicks[msg.nick][3] == "https://webchat.freenode.net":
                        hh = mask.split('@')[1]
                        mask = '*@%s' % hh
                flag = ircdb.makeChannelCapability(channel, 'pattern')
                if ircdb.checkCapability(msg.prefix, flag):
                    for k in i.patterns:
                        pattern = i.patterns[k]
                        if pattern.match(raw):
                            if pattern.limit == 0:
                                isBanned = True
                                uid = random.randint(0,1000000)
                                reason = '%s - matches #%s in %s' % (uid,pattern.uid,channel)
                                log = 'BAD: [%s] %s (matches #%s - %s)' % (channel,msg.prefix,pattern.uid,uid)
                                self.ban(irc,msg.nick,msg.prefix,mask,self.registryValue('klineDuration'),reason,self.registryValue('klineMessage'),log,killReason)
                                i.count(self.getDb(irc.network),pattern.uid)
                                chan.klines.enqueue('%s %s' % (msg.nick.lower(),mask))
                                self.isAbuseOnChannel(irc,channel,'pattern',mask)
                                self.setRegistryValue('lastActionTaken',time.time(),channel=channel)
                                break
                            else:
                                queue = self.getIrcQueueFor(irc,mask,pattern.uid,pattern.life)
                                queue.enqueue(text)
                                if len(queue) > pattern.limit:
                                    isBanned = True
                                    uid = random.randint(0,1000000)
                                    reason = '%s - matches #%s (%s/%ss) in %s' % (uid,pattern.uid,pattern.limit,pattern.life,channel)
                                    log = 'BAD: [%s] %s (matches #%s %s/%ss - %s)' % (channel,msg.prefix,pattern.uid,pattern.limit,pattern.life,uid)
                                    self.ban(irc,msg.nick,msg.prefix,mask,self.registryValue('klineDuration'),reason,self.registryValue('klineMessage'),log,killReason)
                                    self.rmIrcQueueFor(irc,mask)
                                    i.count(self.getDb(irc.network),pattern.uid)
                                    chan.klines.enqueue('%s %s' % (msg.nick.lower(),mask))
                                    self.isAbuseOnChannel(irc,channel,'pattern',mask)
                                    self.setRegistryValue('lastActionTaken',time.time(),channel=channel)
                                    break
                                i.count(self.getDb(irc.network),pattern.uid)
                if isBanned:
                    continue
                if i.defcon and self.isChannelUniSpam(irc,msg,channel,mask,text):
                    isBanned = True
                    uid = random.randint(0,1000000)
                    reason = '!dnsbl UniSpam'
                    log = 'BAD: [%s] %s (%s - %s)' % (channel,msg.prefix,reason,uid)
                    chan.klines.enqueue('%s %s' % (msg.nick.lower(),mask))
                    reason = '%s - %s' % (uid,reason)
                    self.ban(irc,msg.nick,msg.prefix,mask,self.registryValue('klineDuration'),reason,self.registryValue('klineMessage'),log,killReason)
                    self.setRegistryValue('lastActionTaken',time.time(),channel=channel)
                    i.defcon = time.time()
                if isBanned:
                    continue
                ignoreDuration = self.registryValue('ignoreDuration',channel=channel)
                if not msg.nick in chan.nicks:
                    t = time.time()
                    if isCloaked(msg.prefix,self):
                        t = t - ignoreDuration - 1
                    chan.nicks[msg.nick] = [t,msg.prefix,mask]
                isIgnored = False
                if ignoreDuration > 0:
                    ts = chan.nicks[msg.nick][0]
                    if time.time()-ts > ignoreDuration:
                        isIgnored = True
                reason = ''
                publicreason = ''
                if self.registryValue('joinSpamPartPermit',channel=channel) > -1:
                    kind = 'joinSpamPart'
                    life = self.registryValue('joinSpamPartLife',channel=channel)
                    key = mask
                    isNew = False
                    if not kind in chan.buffers:
                        chan.buffers[kind] = {}
                    if not key in chan.buffers[kind]:
                        isNew = True
                        chan.buffers[kind][key] = utils.structures.TimeoutQueue(life)
                    elif chan.buffers[kind][key].timeout != life:
                        chan.buffers[kind][key].setTimeout(life)
                    chan.buffers[kind][key].enqueue(key)
                    if not isIgnored and isNew and len(chan.buffers[kind][key]) == 1 and text.startswith('http') and time.time()-chan.nicks[msg.nick][0] < 15 and 'z' in irc.state.channels[channel].modes and channel == '#freenode':
                        publicreason = 'link spam once joined'
                        reason = 'linkspam'
                badunicode = False
                flag = ircdb.makeChannelCapability(channel,'badunicode')
                if ircdb.checkCapability(msg.prefix,flag):
                    badunicode = self.isChannelUnicode(irc,msg,channel,mask,text)
                    if badunicode and self.hasAbuseOnChannel(irc,channel,'badunicode'):
                        isIgnored = False
                    if badunicode:
                        publicreason = 'unreadable unicode glyphes'
                        reason = badunicode
                hilight = False
                flag = ircdb.makeChannelCapability(channel, 'hilight')
                if ircdb.checkCapability(msg.prefix, flag):
                    hilight = self.isChannelHilight(irc,msg,channel,mask,text)
                    if hilight and self.hasAbuseOnChannel(irc,channel,'hilight'):
                        isIgnored = False
                    if hilight:
                         publicreason = 'nicks/hilight spam'
                         reason = hilight
                if chan.patterns and not len(reason):
                    for pattern in chan.patterns:
                        if pattern in text:
                            isIgnored = False
                            reason = 'matches tmp pattern in %s' % channel
                            publicreason = 'your sentence matches temporary blacklisted words'
                            chan.patterns.enqueue(pattern)
                            self.isAbuseOnChannel(irc,channel,'pattern',mask)
                            break
                massrepeat = False
                flag = ircdb.makeChannelCapability(channel, 'massRepeat')
                if ircdb.checkCapability(msg.prefix, flag):
                    massrepeat = self.isChannelMassRepeat(irc,msg,channel,mask,text)
                    if massrepeat and self.hasAbuseOnChannel(irc,channel,'massRepeat'):
                        isIgnored = False
                lowmassrepeat = False
                flag = ircdb.makeChannelCapability(channel, 'lowMassRepeat')
                if ircdb.checkCapability(msg.prefix, flag):
                    lowmassrepeat = self.isChannelLowMassRepeat(irc,msg,channel,mask,text)
                    if lowmassrepeat and self.hasAbuseOnChannel(irc,channel,'lowMassRepeat'):
                        isIgnored = False
                repeat = False
                flag = ircdb.makeChannelCapability(channel, 'repeat')
                if ircdb.checkCapability(msg.prefix, flag):
                    repeat = self.isChannelRepeat(irc,msg,channel,mask,text)
                    if repeat and self.hasAbuseOnChannel(irc,channel,'repeat'):
                        isIgnored = False
                lowrepeat = False
                flag = ircdb.makeChannelCapability(channel, 'lowRepeat')
                if ircdb.checkCapability(msg.prefix, flag):
                    lowrepeat = self.isChannelLowRepeat(irc,msg,channel,mask,text)
                    if lowrepeat and self.hasAbuseOnChannel(irc,channel,'lowRepeat'):
                        isIgnored = False
                lowhilight = False
                flag = ircdb.makeChannelCapability(channel, 'lowHilight')
                if ircdb.checkCapability(msg.prefix, flag):
                    lowhilight = self.isChannelLowHilight(irc,msg,channel,mask,text)
                    if lowhilight and self.hasAbuseOnChannel(irc,channel,'lowHilight'):
                        isIgnored = False
                flood = False
                flag = ircdb.makeChannelCapability(channel, 'flood')
                if ircdb.checkCapability(msg.prefix, flag):
                    flood = self.isChannelFlood(irc,msg,channel,mask,text)
                    if flood and self.hasAbuseOnChannel(irc,channel,'flood'):
                        isIgnored = False
                lowflood = False
                flag = ircdb.makeChannelCapability(channel, 'lowFlood')
                if ircdb.checkCapability(msg.prefix, flag):
                    lowflood = self.isChannelLowFlood(irc,msg,channel,mask,text)
                    if lowflood and self.hasAbuseOnChannel(irc,channel,'lowFlood'):
                        isIgnored = False
                ctcp = False
                flag = ircdb.makeChannelCapability(channel, 'ctcp')
                if ircdb.checkCapability(msg.prefix, flag):
                    if not ircmsgs.isAction(msg) and ircmsgs.isCtcp(msg):
                        ctcp = self.isChannelCtcp(irc,msg,channel,mask,text)
                    if ctcp and self.hasAbuseOnChannel(irc,channel,'ctcp'):
                        isIgnored = False
                notice = False
                flag = ircdb.makeChannelCapability(channel, 'notice')
                if ircdb.checkCapability(msg.prefix, flag):
                    if not ircmsgs.isAction(msg) and isNotice:
                        notice = self.isChannelNotice(irc,msg,channel,mask,text)
                    if notice and self.hasAbuseOnChannel(irc,channel,'notice'):
                        isIgnored = False
                cap = False
                flag = ircdb.makeChannelCapability(channel, 'cap')
                if ircdb.checkCapability(msg.prefix, flag):
                    cap = self.isChannelCap(irc,msg,channel,mask,raw)
                    if cap and self.hasAbuseOnChannel(irc,channel,'cap'):
                        isIgnored = False
                if not reason:
                    if massrepeat:
                        reason = massrepeat
                        publicreason = 'repetition detected'
                    elif lowmassrepeat:
                        reason = lowmassrepeat
                        publicreason = 'repetition detected'
                    elif repeat:
                        reason = repeat
                        publicreason = 'repetition detected'
                    elif lowrepeat:
                        reason = lowrepeat
                        publicreason = 'repetition detected'
                    elif hilight:
                        reason = hilight
                        publicreason = 'nicks/hilight spam'
                    elif lowhilight:
                        reason = lowhilight
                        publicreason = 'nicks/hilight spam'
                    elif cap:
                        reason = cap
                        publicreason = 'uppercase detected'
                    elif flood:
                        reason = flood
                        publicreason = 'flood detected'
                    elif lowflood:
                        reason = lowflood
                        publicreason = 'flood detected'
                    elif ctcp:
                        reason = ctcp
                        publicreason = 'channel CTCP'
                    elif notice:
                        reason = notice
                        publicreason = 'channel notice'
                if reason:
                    if isIgnored:
                        if self.warnedOnOtherChannel(irc,channel,mask):
                            isIgnored = False
                        elif self.isBadOnChannel(irc,channel,'bypassIgnore',mask):
                            isIgnored = False
                    if chan.called:
                        isIgnored = False
                    if isIgnored:
                        bypassIgnore = self.isBadOnChannel(irc,channel,'bypassIgnore',mask)
                        if bypassIgnore:
                            isBanned = True
                            uid = random.randint(0,1000000)
                            reason = '%s %s' % (reason,bypassIgnore)
                            log = 'BAD: [%s] %s (%s - %s)' % (channel,msg.prefix,reason,uid)
                            chan.klines.enqueue('%s %s' % (msg.nick.lower(),mask))
                            reason = '%s - %s' % (uid,reason)
                            self.ban(irc,msg.nick,msg.prefix,mask,self.registryValue('klineDuration'),reason,self.registryValue('klineMessage'),log,killReason)
                            self.setRegistryValue('lastActionTaken',time.time(),channel=channel)
                            if i.defcon:
                                i.defcon = time.time()
                        else:
                            q = self.getIrcQueueFor(irc,mask,'warned-%s' % channel,self.registryValue('alertPeriod'))
                            if len(q) == 0:
                                q.enqueue(text)
                                self.logChannel(irc,'IGNORED: [%s] %s (%s)' % (channel,msg.prefix,reason))
                                matter = None
                                if msg.nick:
                                    irc.queueMsg(ircmsgs.notice(msg.nick,"Your actions in %s tripped automated anti-spam measures (%s), but were ignored based on your time in channel. Stop now, or automated action will still be taken. If you have any questions, please don't hesitate to contact a member of staff" % (channel,publicreason)))
                    else:
                        isBanned = True
                        uid = random.randint(0,1000000)
                        log = 'BAD: [%s] %s (%s - %s)' % (channel,msg.prefix,reason,uid)
                        chan.klines.enqueue('%s %s' % (msg.nick.lower(),mask))
                        reason = '%s - %s' % (uid,reason)
                        self.ban(irc,msg.nick,msg.prefix,mask,self.registryValue('klineDuration'),reason,self.registryValue('klineMessage'),log,killReason)
                        if i.defcon:
                            i.defcon = time.time()
                        if chan.called:
                            chan.called = time.time()
                        if i.lastDefcon and time.time()-i.lastDefcon < self.registryValue('alertPeriod') and not i.defcon:
                            self.logChannel(irc,"INFO: ignores lifted and abuses end to klines for %ss due to abuses in %s after lastest defcon %s" % (self.registryValue('defcon')*2,channel,i.lastDefcon))
                            i.defcon = time.time() + (self.registryValue('defcon')*2)
                            if not i.god:
                                irc.sendMsg(ircmsgs.IrcMsg('MODE %s +p' % irc.nick))
                            else:
                                self.applyDefcon(irc)
                        ip = mask.split('@')[1]
                        if hilight and i.defcon:
                            if utils.net.bruteIsIPV6(ip) or utils.net.isIPV4(ip):
                                if len(self.registryValue('droneblKey')) and len(self.registryValue('droneblHost')) and self.registryValue('enable'):
                                    t = world.SupyThread(target=self.fillDnsbl,name=format('fillDnsbl %s', ip),args=(irc,ip,self.registryValue('droneblHost'),self.registryValue('droneblKey'),reason))
                                    t.setDaemon(True)
                                    t.start()
                        self.setRegistryValue('lastActionTaken',time.time(),channel=channel)

                if not isBanned:
                    mini = self.registryValue('amsgMinimum')
                    if len(text) > mini or text.find('http') != -1:
                        limit = self.registryValue('amsgPermit')
                        if limit > -1:
                            life = self.registryValue('amsgLife')
                            percent = self.registryValue('amsgPercent')
                            queue = self.getIrcQueueFor(irc,mask,channel,life)
                            queue.enqueue(text)
                            found = None
                            for ch in i.channels:
                                chc = self.getChan(irc,ch)
                                if msg.nick in chc.nicks and ch != channel:
                                    queue = self.getIrcQueueFor(irc,mask,ch,life)
                                    for m in queue:
                                        if compareString(m,text) > percent:
                                            found = ch
                                            break
                                    if found:
                                        break
                            if found:
                                queue = self.getIrcQueueFor(irc,mask,'amsg',life)
                                flag = False
                                for q in queue:
                                    if found in q:
                                        flag = True
                                        break
                                if not flag:
                                    queue.enqueue(found)
                                if len(queue) > limit:
                                    chs = list(queue)
                                    queue.reset()
                                    key = 'amsg %s' % mask
                                    q = self.getIrcQueueFor(irc,key,'amsg',self.registryValue('alertPeriod'))
                                    if len(q) == 0:
                                        q.enqueue(mask)
                                        chs.append(channel)
                                        self.logChannel(irc,'AMSG: %s (%s) in %s' % (msg.nick,text,', '.join(chs)))
                                        for channel in i.channels:
                                            chan = self.getChan(irc,channel)
                                            life = self.registryValue('computedPatternLife',channel=channel)
                                            if not chan.patterns:
                                                chan.patterns = utils.structures.TimeoutQueue(life)
                                            elif chan.patterns.timeout != life:
                                                chan.patterns.setTimeout(life)
                                            chan.patterns.enqueue(text.lower())

    def handleSecretMessage (self,irc,msg):
        (targets, text) = msg.args
        nicks = ['OperServ','NickServ']
        i = self.getIrc(irc)
        if msg.nick in nicks:
            if text.startswith('klinechan_check_join(): klining '):
                patterns = self.registryValue('droneblPatterns')
                found = False
                if len(patterns):
                    for pattern in patterns:
                        if len(pattern) and pattern in text:
                            found = pattern
                            break
                    if found:
                        a = text.split('klinechan_check_join(): klining ')[1].split(' ')
                        a = a[0]
                        ip = a.split('@')[1]
                        if utils.net.isIPV4(ip) or utils.net.bruteIsIPV6(ip):
                            if len(self.registryValue('droneblKey')) and len(self.registryValue('droneblHost')) and self.registryValue('enable'):
                                t = world.SupyThread(target=self.fillDnsbl,name=format('fillDnsbl %s', ip),args=(irc,ip,self.registryValue('droneblHost'),self.registryValue('droneblKey'),found))
                                t.setDaemon(True)
                                t.start()
                            else:
                                self.prefixToMask(irc,'*!*@%s' % ip,'',True)
            if text.startswith('sendemail():') and self.registryValue('registerPermit') > 0:
               text = text.replace('sendemail():','')
               pattern = r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})'
               result = re.search(pattern,text)
               email = text.split('<')[1].split('>')[0]
               h = text.split('email for ')[1].split(']')[0].strip().replace('[','!')
               if result:
                   ip = result.group(0)
                   if ip and 'type register to' in text:
                       q = self.getIrcQueueFor(irc,ip,'register',self.registryValue('registerLife'))
                       q.enqueue(email)
                       if len(q) > self.registryValue('registerPermit'):
                           ms = []
                           for m in q:
                               ms.append(m)
                           if i.defcon:
                               uid = random.randint(0,1000000)
                               m = self.prefixToMask(irc,h)
                               self.ban(irc,nick,h,m,self.registryValue('klineDuration'),'%s - services load with %s' % (uid,','.join(ms)),self.registryValue('klineMessage'),'BAD: %s (registered load of accounts - %s)' % (h,uid))
                           else:
                               self.logChannel(irc,'SERVICE: %s load of accounts %s' % (h,', '.join(ms)))
               if 'type register to' in text:
                   q = self.getIrcQueueFor(irc,email,'register',self.registryValue('registerLife'))
                   text = text.replace('email for ','')
                   text = text.split(' type register')[0]
                   q.enqueue(text.strip())
                   if len(q) > self.registryValue('registerPermit'):
                       ms = []
                       for m in q:
                           ms.append(q)
                       self.logChannel(irc,'SERVICE: loads of registration to %s (%s)' % (email,', '.join(ms)))
            if 'AKICK:ADD:' in text or 'AKICK:DEL:' in text:
               life = self.registryValue('decloakLife')
               limit = self.registryValue('decloakPermit')
               if limit > -1:
                   origin = text.split(' ')[0]
                   target = text.split(' ').pop()
                   q = self.getIrcQueueFor(irc,origin,target,life)
                   q.enqueue(text)
                   if len(q) > limit:
                       q.reset()
                       self.logChannel(irc,'SERVICE: [%s] %s suspicious AKICK behaviour' % (target,origin))
            if 'VERIFY:EMAILCHG:' in text:
                account = text.split(' VERIFY:EMAILCHG')[0]
                email = text.split('(email: ')[1].split(')')[0].split('@')[1]
                t = world.SupyThread(target=self.resolveSnoopy,name=format('Snoopy %s', email),args=(irc,account,email,True,True))
                t.setDaemon(True)
                t.start()

    def handleReportMessage (self,irc,msg):
        (targets, text) = msg.args
        nicks = self.registryValue('reportNicks')
        if msg.nick in nicks:
            i = self.getIrc(irc)
            if text.startswith('BAD:') and not '(tor' in text and '(' in text:
                permit = self.registryValue('reportPermit')
                if permit > -1:
                    life = self.registryValue('reportLife')
                    queue = self.getIrcQueueFor(irc,'report','bad',life)
                    target = text.split('(')[0]
                    if len(text.split(' ')) > 1:
                        target = text.split(' ')[1]
                    found = False
                    for q in queue:
                        if q == target:
                            found = True
                            break
                    if not found:
                        queue.enqueue(target)
                        if len(queue) > permit:
                            queue.reset()
                            if not i.defcon:
                                self.logChannel(irc,"BOT: Wave in progress (%s/%ss), ignores lifted, triggers thresholds lowered for %ss at least" % (self.registryValue('reportPermit'),self.registryValue('reportLife'),self.registryValue('defcon')))
                                i.defcon = time.time()
                                if not i.god:
                                    irc.sendMsg(ircmsgs.IrcMsg('MODE %s +p' % irc.nick))
                                else:
                                    self.applyDefcon (irc)
                            i.defcon = time.time()
            else:
                if i.netsplit and text.startswith('Join rate in '):
                    i.netsplit = time.time() + self.registryValue('netsplitDuration')
                if text.startswith('Client ') and 'suspicious' in text and i.defcon:
                    text = text.replace('Client ','')
                    hostmask = text.split(' ')[0].replace('(','!').replace(')','')
                    if ircutils.isUserHostmask(hostmask):
                        mask = self.prefixToMask(irc,hostmask)
                        (nick,ident,host) = ircutils.splitHostmask(hostmask)
                        patterns = self.registryValue('droneblPatterns')
                        found = False
                        if len(patterns):
                            for pattern in patterns:
                                if len(pattern) and pattern in text:
                                    found = pattern
                                    break
                        if found:
                            def k():
                                self.kline(irc,hostmask,mask,self.registryValue('klineDuration'),'!dnsbl (%s in suspicious mask)' % found)
                            schedule.addEvent(k,time.time()+random.uniform(1, 6))
                if text.startswith('Killing client ') and 'due to lethal mask ' in text:
                    patterns = self.registryValue('droneblPatterns')
                    found = False
                    if len(patterns):
                        for pattern in patterns:
                            if len(pattern) and pattern in text:
                                found = pattern
                                break
                    if found:
                        a = text.split('Killing client ')[1]
                        a = a.split(')')[0]
                        ip = a.split('@')[1]
                        if utils.net.isIPV4(ip) or utils.net.bruteIsIPV6(ip):
                            if len(self.registryValue('droneblKey')) and len(self.registryValue('droneblHost')) and self.registryValue('enable'):
                                t = world.SupyThread(target=self.fillDnsbl,name=format('fillDnsbl %s', ip),args=(irc,ip,self.registryValue('droneblHost'),self.registryValue('droneblKey'),found))
                                t.setDaemon(True)
                                t.start()
                        else:
                            self.prefixToMask(irc,'*!*@%s' % ip,'',True,found)

    def doPrivmsg (self,irc,msg):
        self.handleMsg(irc,msg,False)
        try:
            i = self.getIrc(irc)
            mask = self.prefixToMask(irc,msg.prefix)
            (targets, text) = msg.args
            text = text
            if ircdb.checkCapability(msg.prefix, 'protected'):
                return
            for channel in targets.split(','):
                if channel.startswith('@'):
                    channel = channel.replace('@','',1)
                if channel.startswith('+'):
                    channel = channel.replace('+','',1)
                if not irc.isChannel(channel) and channel == irc.nick:
                    killReason = self.registryValue('killMessage',channel=channel)
                    for k in i.patterns:
                        pattern = i.patterns[k]
                        if pattern.match(text):
                            if pattern.limit == 0:
                                uid = random.randint(0,1000000)
                                reason = '%s - matches #%s in pm' % (pattern.uid,uid)
                                log = 'BAD: [%s] %s (matches #%s - %s)' % (channel,msg.prefix,pattern.uid,uid)
                                self.ban(irc,msg.nick,msg.prefix,mask,self.registryValue('klineDuration'),reason,self.registryValue('klineMessage'),log,killReason)
                                i.count(self.getDb(irc.network),pattern.uid)
                                break
                            else:
                                queue = self.getIrcQueueFor(irc,mask,pattern.uid,pattern.life)
                                queue.enqueue(text)
                                if len(queue) > pattern.limit:
                                    uid = random.randint(0,1000000)
                                    reason = '%s - matches #%s (%s/%ss) in pm' % (pattern.uid,pattern.limit,pattern.life,uid)
                                    log = 'BAD: [%s] %s (matches #%s %s/%ss - %s)' % (channel,msg.prefix,pattern.uid,pattern.limit,pattern.life,uid)
                                    self.ban(irc,msg.nick,msg.prefix,mask,self.registryValue('klineDuration'),reason,self.registryValue('klineMessage'),log,killReason)
                                    self.rmIrcQueueFor(irc,mask)
                                    i.count(self.getDb(irc.network),pattern.uid)
                                    break
                                i.count(self.getDb(irc.network),pattern.uid)
        except:
            return

    def doTopic(self, irc, msg):
        self.handleMsg(irc,msg,False)

    def do903 (self,irc,msg):
        irc.queueMsg(ircmsgs.IrcMsg('CAP REQ :extended-join account-notify'))

    def handleFloodSnote (self,irc,text):
        user = text.split('Possible Flooder ')[1]
        a = user[::-1]
        ar = a.split(']',1)
        ar.reverse()
        ar.pop()
        user = "%s" % ar[0]
        user = user.replace('[','!',1)
        user = '%s' % user[::-1]
        if not ircutils.isUserHostmask(user):
            return
        target = text.split('target: ')[1]
        i = self.getIrc(irc)
        if irc.isChannel(target):
            limit = self.registryValue('channelFloodPermit')
            life = self.registryValue('channelFloodLife')
            key = 'snoteFloodAlerted'
            if limit > -1:
                if not self.registryValue('ignoreChannel',target):
                    protected = ircdb.makeChannelCapability(target, 'protected')
                    if not ircdb.checkCapability(user, protected):
                        queue = self.getIrcQueueFor(irc,target,'snoteFlood',life)
                        if i.defcon:
                            if limit > 0:
                                limit = limit - 1
                        stored = False
                        for u in queue:
                            if u == user:
                                stored = True
                                break
                        if not stored:
                            queue.enqueue(user)
                        users = list(queue)
                        if len(queue) > limit:
                            self.logChannel(irc,'NOTE: [%s] is flooded by %s' % (target,', '.join(users)))
                            queue.reset()
                            queue = self.getIrcQueueFor(irc,target,'snoteFloodJoin',life)
                            queue.enqueue(text)
                            if len(queue) > 1 or i.defcon:
                                if self.registryValue('lastActionTaken',channel=target) > 0.0 and not target in irc.state.channels:
                                    for user in users:
                                        if not 'gateway/web/' in user:
                                            mask = self.prefixToMask(irc,user)
                                            uid = random.randint(0,1000000)
                                            self.kline(irc,user,mask,self.registryValue('klineDuration'),'%s - snote flood on %s' % (uid,target))
                                            self.logChannel(irc,"BAD: %s (snote flood on %s - %s)" % (user,target,uid))
                                    t = time.time() - (self.registryValue('leaveChannelIfNoActivity',channel=target) * 24 * 3600) + 1800
                                    self.setRegistryValue('lastActionTaken',t,channel=target)
                                    irc.sendMsg(ircmsgs.join(target))
                                    self.logChannel(irc,"JOIN: [%s] due to flood snote" % target)
                                    try:
                                        network = conf.supybot.networks.get(irc.network)
                                        network.channels().add(target)
                                    except KeyError:
                                        pass
                                queue.reset()
        else:
            limit = self.registryValue('userFloodPermit')
            life = self.registryValue('userFloodLife')
            if limit > -1:
                if target.startswith('freenode-connect'):
                    return
                queue = self.getIrcQueueFor(irc,target,'snoteFlood',life)
                stored = False
                for u in queue:
                    if u == user:
                        stored = True
                        break
                if not stored:
                    queue.enqueue(user)
                users = list(queue)
                if len(queue) > limit:
                    queue.reset()
                    queue = self.getIrcQueueFor(irc,target,'snoteFloodLethal',life)
                    queue.enqueue(','.join(users))
                    if i.defcon or len(queue) > 1:
                        for m in queue:
                            for q in m.split(','):
                                if not (ircdb.checkCapability(q, 'protected') or target == 'freenode-connect'):
                                    mask = self.prefixToMask(irc,q)
                                    uid = random.randint(0,1000000)
                                    self.kline(irc,q,mask,self.registryValue('klineDuration'),'%s - snote flood on %s' % (uid,target))
                                    self.logChannel(irc,"BAD: %s (snote flood on %s - %s)" % (q,target,uid))
                    else:
                        self.logChannel(irc,'NOTE: %s is flooded by %s' % (target,', '.join(users)))
                if ircdb.checkCapability(user, 'protected'):
                    return
                queue = self.getIrcQueueFor(irc,user,'snoteFlood',life)
                stored = False
                for u in queue:
                    if u == target:
                        stored = True
                        break
                if not stored:
                    queue.enqueue(target)
                if len(queue)> limit:
                    targets = list(queue)
                    queue.reset()
                    queue = self.getIrcQueueFor(irc,user,'snoteFloodLethal',life)
                    queue.enqueue(target)
                    if i.defcon or len(queue) > 1:
                         mask = self.prefixToMask(irc,user)
                         uid = random.randint(0,1000000)
                         self.kline(irc,user,mask,self.registryValue('klineDuration'),'%s - snote flood %s' % (uid,','.join(targets)))
                         self.logChannel(irc,"BAD: %s (snote flood %s - %s)" % (user,','.join(targets),uid))
                    else:
                        self.logChannel(irc,'NOTE: %s is flooding %s' % (user,', '.join(targets)))

    def handleJoinSnote (self,irc,text):
        limit = self.registryValue('joinRatePermit')
        life = self.registryValue('joinRateLife')
        target = text.split('trying to join ')[1].split(' is')[0]
        if self.registryValue('ignoreChannel',target):
            return
        user = text.split('User ')[1].split(')')[0]
        user = user.replace('(','!').replace(')','').replace(' ','')
        if not ircutils.isUserHostmask(user):
            return
        mask = self.prefixToMask(irc,user)
        if ircdb.checkCapability(user, 'protected'):
            return
        protected = ircdb.makeChannelCapability(target, 'protected')
        if ircdb.checkCapability(user, protected):
            return
        queue = self.getIrcQueueFor(irc,user,'snoteJoin',life)
        stored = False
        for u in queue:
            if u == user:
                stored = True
                break
        if not stored:
            queue.enqueue(user)
        i = self.getIrc(irc)
        key = 'snoteJoinAlerted'
        if len(queue) > limit and limit > 0:
            users = list(queue)
            queue.reset()
            queue = self.getIrcQueueFor(irc,user,'snoteJoinAlert',self.registryValue('alertPeriod'))
            if len(queue):
               self.logChannel(irc,'NOTE: [%s] join/part by %s' % (target,', '.join(users)))
            queue.enqueue(','.join(users))
        life = self.registryValue('crawlLife')
        limit = self.registryValue('crawlPermit')
        if limit < 0:
            return
        queue = self.getIrcQueueFor(irc,mask,'snoteJoin',life)
        stored = False
        for u in queue:
            if u == target:
                stored = True
                break
        if not stored:
            queue.enqueue(target)
        if '1wm' in user:
            limit = 1
        if len(queue) > limit:
            channels = list(queue)
            queue.reset()
            queue = self.getIrcQueueFor(irc,mask,'snoteJoinLethal',self.registryValue('alertPeriod'))
            if len(queue) == 0:
                self.logChannel(irc,'NOTE: %s is indexing the network (%s)' % (user,', '.join(channels)))
                queue.enqueue(mask)
            else:
                self.kline(irc,user,mask,self.registryValue('klineDuration'),'crawling')

    def handleIdSnote (self,irc,text):
        target = text.split('failed login attempts to ')[1].split('.')[0].strip()
        user = text.split('Last attempt received from ')[1].split(' on')[0].strip()
        if not ircutils.isUserHostmask(user):
            return
        if user.split('!')[0].lower() == target.lower():
            return
        limit = self.registryValue('idPermit')
        life = self.registryValue('idLife')
        if limit < 0:
            return
        queue = self.getIrcQueueFor(irc,user,'snoteId',life)
        queue.enqueue(target)
        i = self.getIrc(irc)
        targets = []
        key = 'snoteIdAlerted'
        if len(queue) > limit:
            targets = list(queue)
            queue.reset()
            if not key in i.queues[user]:
                def rcu():
                    i = self.getIrc(irc)
                    if user in i.queues:
                        if key in i.queues[user]:
                            del i.queues[user][key]
                i.queues[user][key] = time.time()
                schedule.addEvent(rcu,time.time()+self.registryValue('abuseLife'))
        if key in i.queues[user]:
            if len(queue):
                targets = list(queue)
                queue.reset()
            a = []
            for t in targets:
                if not t in a:
                    a.append(t)
            mask = self.prefixToMask(irc,user)
            (nick,ident,host) = ircutils.splitHostmask(user)
            if not mask in i.klines:
                uid = random.randint(0,1000000)
                privateReason = '%s - ns id flood (%s)' % (uid,', '.join(a))
                if i.defcon:
                    privateReason = '!dnsbl ' + privateReason
                self.kline(irc,user,mask,self.registryValue('klineDuration'), privateReason)
                self.logChannel(irc,"BAD: %s (%s)" % (user,privateReason))
        queue = self.getIrcQueueFor(irc,target,'snoteId',life)
        queue.enqueue(user)
        targets = []
        if len(queue) > limit:
            targets = list(queue)
            queue.reset()
            def rct():
                i = self.getIrc(irc)
                if target in i.queues:
                    if key in i.queues[target]:
                        del i.queues[target][key]
            i.queues[target][key] = time.time()
            schedule.addEvent(rct,time.time()+self.registryValue('abuseLife'))
        if key in i.queues[target]:
            if len(queue):
                targets = list(queue)
                queue.reset()
            a = {}
            for t in targets:
                if not t in a:
                    a[t] = t
            for u in a:
                mask = self.prefixToMask(irc,u)
                (nick,ident,host) = ircutils.splitHostmask(u)
                if not mask in i.klines:
                    self.kill(irc,nick,self.registryValue('killMessage'))
                    uid = random.randint(0,1000000)
                    privateReason = '%s - ns id flood on %s' % (uid,target)
                    if i.defcon:
                        privateReason = '!dsnbl ' + privateReason
                    self.kline(irc,u,mask,self.registryValue('klineDuration'), privateReason)
                    self.logChannel(irc,"BAD: %s (%s)" % (u,privateReason))

    def handleKline(self,irc,text):
        i = self.getIrc(irc)
        user = text.split('active for')[1]
        a = user[::-1]
        ar = a.split(']',1)
        ar.reverse()
        ar.pop()
        user = "%s" % ar[0]
        user = user.replace('[','!',1)
        user = '%s' % user[::-1]
        user = user.strip()
        if not ircutils.isUserHostmask(user):
            return
        (nick,ident,host) = ircutils.splitHostmask(user)
        permit = self.registryValue('alertOnWideKline')
        found = ''
        if not i.lastKlineOper.find('freenode/staff/') == -1:
            for channel in i.channels:
                chan = i.channels[channel]
                ns = []
                if nick in chan.nicks:
                    if len(chan.nicks[nick]) == 5:
                        if chan.nicks[nick][4] and chan.nicks[nick][1] == user:
                            found = chan.nicks[nick][4]
                            break
        if found:
            self.log.info ('Account klined %s --> %s' % (found,user))
        if permit > -1:
            if '/' in host:
                if host.startswith('gateway/') or host.startswith('nat/'):
                    h = host.split('/')
                    h[-1] = '*'
                    host = '/'.join(h)
            ranges = self._ip_ranges(host)
            announced = False
            for range in ranges:
                range = range
                queue = self.getIrcQueueFor(irc,range,'klineNote',7)
                queue.enqueue(user)
                if len(queue) == permit:
                    if not announced:
                        announced = True
                        self.logChannel(irc,"NOTE: a kline similar to *@%s seems to hit more than %s users" % (range,self.registryValue('alertOnWideKline')))

    def handleNickSnote (self,irc,text):
        text = text.replace('Nick change: From ','')
        text = text.split(' to ')[1]
        nick = text.split(' ')[0]
        host = text.split(' ')[1]
        host = host.replace('[','',1)
        host = host[:-1]
        limit = self.registryValue('nickChangePermit')
        life = self.registryValue('nickChangeLife')
        if limit < 0:
            return
        mask = self.prefixToMask(irc,'%s!%s' % (nick,host))
        i = self.getIrc(irc)
        if not i.defcon:
            return
        queue = self.getIrcQueueFor(irc,mask,'snoteNick',life)
        queue.enqueue(nick)
        if len(queue) > limit:
            nicks = list(queue)
            queue.reset()
            uid = random.randint(0,1000000)
            self.kline(irc,'%s!%s' % (nick,host),mask,self.registryValue('klineDuration'),'%s - nick changes abuses %s/%ss' % (uid,limit,life))
            self.logChannel(irc,"BAD: %s abuses nick change (%s - %s)" % (mask,','.join(nicks),uid))

    def handleChannelCreation (self,irc,text):
        text = text.replace(' is creating new channel ','')
        permit = self.registryValue('channelCreationPermit')
        user = text.split('#')[0]
        channel = '#' + text.split('#')[1]
        if '##' in text:
            channel = '##' + text.split('##')[1]
        i = self.getIrc(irc)
        if len(self.registryValue('lethalChannels')) > 0:
            for pattern in self.registryValue('lethalChannels'):
                if len(pattern) and pattern in channel and not user in channel and not user in i.tokline:
                    i.toklineresults[user] = {}
                    i.toklineresults[user]['kind'] = 'lethal'
                    i.tokline[user] = text
                    self.log.info('WHOIS %s (%s)' % (user,channel))
                    irc.sendMsg(ircmsgs.IrcMsg('WHOIS %s %s' % (user,user)))
                    break

    def handleClient (self,irc,text):
        i = self.getIrc(irc)

        #if i.defcon:


    def doNotice (self,irc,msg):
        (targets, text) = msg.args
        if len(targets) and targets[0] == '*':
            # server notices
            text = text.replace('\x02','')
            if text.startswith('*** Notice -- '):
                text = text.replace('*** Notice -- ','')
            if text.startswith('Client connecting'):
                if 'gateway/vpn/privateinternetaccess' in text:
                    account = text.split('(')[1].split(')')[0]
                    account = account.split('@gateway/vpn/privateinternetaccess/')[1].split('/')[0]
                    #self.log.info('connecting %s' % account)
                    q = self.getIrcQueueFor(irc,account,'nsregister',600)
                    if len(q) == 1:
                        self.logChannel(irc,"SERVICE: fresh account %s moved to pia" % account)
            if text.startswith('Possible Flooder '):
                self.handleFloodSnote(irc,text)
            #elif text.find('is creating new channel') != -1:
            #    self.handleChannelCreation(irc,text)
            elif text.startswith('Nick change: From'):
                self.handleNickSnote(irc,text)
            elif text.startswith('User') and text.endswith('is a possible spambot'):
                self.handleJoinSnote(irc,text)
            elif 'failed login attempts to' in text and not 'SASL' in text:
                self.handleIdSnote(irc,text)
            elif text.startswith('Too many clients, rejecting ') or text.startswith('All connections in use.') or text.startswith('creating SSL/TLS socket pairs: 24 (Too many open files)'):
                i = self.getIrc(irc)
                if not msg.prefix in i.limits or time.time() - i.limits[msg.prefix] > self.registryValue('alertPeriod'):
                    i.limits[msg.prefix] = time.time()
                    self.logChannel(irc,'INFRA: %s is rejecting clients' % msg.prefix.split('.')[0])
                if not i.netsplit:
                    self.logChannel(irc,'INFO: netsplit activated for %ss : some abuses are ignored' % self.registryValue('netsplitDuration'))
                i.netsplit = time.time() + self.registryValue('netsplitDuration')
            elif text.startswith('KLINE active') or text.startswith('K/DLINE active'):
                self.handleKline(irc,text)
            elif text.find('due to too high load') != -1:
                i = self.getIrc(irc)
                if not 'services.' in i.limits:
                    i.limits['services.'] = time.time()
                    reason = text.split("type '")[1]
                    reason = reason.split(' ')[0]
                    self.logChannel(irc,"INFRA: High load on services ('%s)" % reason)
                    def rct():
                        i = self.getIrc(irc)
                        if 'services.' in i.limits:
                            del i.limits['services.']
                    schedule.addEvent(rct,time.time()+self.registryValue('alertPeriod'))
            elif 'K-Line for [*@' in text:
                oper = text.split(' ')[0]
                i = self.getIrc(irc)
                i.lastKlineOper = oper
                reason = text.split('K-Line for [*@')[1]
                reason = reason.split(']')[1].replace('[','').replace(']','')
                hasPattern = False
                for p in self.registryValue('droneblPatterns'):
                     if p in reason:
                         hasPattern = p
                         break
                ip = text.split('K-Line for [*@')[1].split(']')[0]
                permit = self.registryValue('ipv4AbusePermit')
                if not 'evilmquin' in oper and permit > -1:
                    ranges = self._ip_ranges(ip)
                    for range in ranges:
                        range = range
                        q = self.getIrcQueueFor(irc,'klineRange',range,self.registryValue('ipv4AbuseLife'))
                        q.enqueue(ip)
                        if len(q) > permit:
                            hs = []
                            for m in q:
                                hs.append(m)
                            q.reset()
                            uid = random.randint(0,1000000)
                            if self.registryValue('useOperServ'):
                                irc.sendMsg(ircmsgs.IrcMsg('PRIVMSG OperServ :AKILL ADD %s !T %s %s' % (range,self.registryValue('klineDuration'),'%s - repeat abuses on this range (%s/%ss)' % (uid,permit,self.registryValue('ipv4AbuseLife')))))
                            else:
                                irc.sendMsg(ircmsgs.IrcMsg('KLINE %s *@%s :%s|%s' % (self.registryValue('klineDuration'),range,self.registryValue('klineMessage'),'%s - repeat abuses on this range (%s/%ss)' % (uid,permit,self.registryValue('ipv4AbuseLife')))))
                            self.logChannel(irc,"BAD: abuses detected on %s (%s/%ss - %s) %s" % (range,permit,self.registryValue('ipv4AbuseLife'),uid,','.join(hs)))
                        permit = permit + 1
                if '!dnsbl' in text or hasPattern:
                    reason = ''
                    if '!dnsbl' in text:
                        reason = text.split('!dnsbl')[1].replace(']','').strip()
                    else:
                        reason = hasPattern
                    if utils.net.isIPV4(ip) or utils.net.bruteIsIPV6(ip):
                        if len(self.registryValue('droneblKey')) and len(self.registryValue('droneblHost')) and self.registryValue('enable'):
                            t = world.SupyThread(target=self.fillDnsbl,name=format('fillDnsbl %s', ip),args=(irc,ip,self.registryValue('droneblHost'),self.registryValue('droneblKey'),reason))
                            t.setDaemon(True)
                            t.start()
                    else:
                        if len(self.registryValue('droneblKey')) and len(self.registryValue('droneblHost')) and self.registryValue('enable'):
                            t = world.SupyThread(target=self.resolve,name=format('resolve %s', '*!*@%s' % ip),args=(irc,'*!*@%s' % ip,'',True, reason))
                            t.setDaemon(True)
                            t.start()
                        else:
                            self.prefixToMask(irc,'*!*@%s' % ip,'',True,reason)
            elif 'failed login attempts to' in text and 'SASL' in text:
                self.handleSaslFailure(irc,text)
            elif text.startswith('FILTER'):
                ip = text.split(' ')[2].split('[')[1].split(']')[0]
                if utils.net.isIPV4(ip) or utils.net.bruteIsIPV6(ip):
                    if not ip in self.ipfiltered:
                        if self.registryValue('serverFilteringPermit') > -1:
                            q = self.getIrcQueueFor(irc,'serverSideFiltering',ip,self.registryValue('serverFilteringLife'))
                            q.enqueue(ip)
                            reason = 'Server Side Filtering'
                            if len(q) > self.registryValue('serverFilteringPermit'):
                                self.ipfiltered[ip] = True
                                if len(self.registryValue('droneblKey')) and len(self.registryValue('droneblHost')) and self.registryValue('enable'):
                                    t = world.SupyThread(target=self.fillDnsbl,name=format('fillDnsbl %s', ip),args=(irc,ip,self.registryValue('droneblHost'),self.registryValue('droneblKey'),reason))
                                    t.setDaemon(True)
                                    t.start()
        else:
            self.handleMsg(irc,msg,True)

    def do215 (self,irc,msg):
        i = self.getIrc(irc)
        if msg.args[0] == irc.nick and msg.args[1] == 'I':
            i.lines[msg.args[4]] = '%s %s %s' % (msg.args[2],msg.args[3],msg.args[5])
#            if len(i.dlines):
#                h = i.dlines.pop(0)
#                self.log.info('DLINE %s|%s' % (h,self.registryValue('saslDuration')))
#                irc.sendMsg(ircmsgs.IrcMsg('DLINE %s %s on * :%s' % (self.registryValue('saslDuration'),h,self.registryValue('saslMessage'))))
#            if len(i.dlines):
#                irc.queueMsg(ircmsgs.IrcMsg('TESTLINE %s' % i.dlines[0]))

    def handleSaslFailure (self,irc,text):
        i = self.getIrc(irc)
        limit = self.registryValue('saslPermit')
        if limit < 0:
            return
        life = self.registryValue('saslLife')
        account = text.split('failed login attempts to ')[1].split('.')[0]
        host = text.split('<Unknown user (via SASL):')[1].split('>')[0]
        q = self.getIrcQueueFor(irc,'sasl',account,life)
        q.enqueue(host)
        hosts = {}
        if len(q) > limit:
            for ip in q:
                hosts[ip] = ip
            q.reset()
        q = self.getIrcQueueFor(irc,'sasl',host,life)
        q.enqueue(account)
        if len(q) > limit:
            q.reset()
            hosts[host] = host
        if self.registryValue('enable'):
            if len(hosts) > 0:
                for h in hosts:
                    if len(i.dlines):
                        i.dlines.append(h)
                    else:
                        i.dlines.append(h)
                        found = None
                        users = None
                        i = self.getIrc(irc)
                        for server in i.servers:
                            if not users or users < i.servers[server]:
                                found = server
                                users = i.servers[server]
                        if found:
                            irc.queueMsg(ircmsgs.IrcMsg('stats I %s' % found))
                            self.logChannel(irc,'NOTE: %s (%s) (%s/%ss)' % (h,'SASL failures',limit,life))

    def warnedOnOtherChannel (self,irc,channel,mask):
        for chan in list(irc.state.channels):
            if chan != channel:
                if self.hasAbuseOnChannel(irc,chan,mask):
                    return True
        return False

    def hasAbuseOnChannel (self,irc,channel,key):
        chan = self.getChan(irc,channel)
        kind = 'abuse'
        limit = self.registryValue('%sPermit' % kind,channel=channel)
        if kind in chan.buffers:
            if key in chan.buffers[kind]:
                if len(chan.buffers[kind][key]) > limit:
                    return True
        return False

    def isAbuseOnChannel (self,irc,channel,key,mask):
        chan = self.getChan(irc,channel)
        kind = 'abuse'
        limit = self.registryValue('%sPermit' % kind,channel=channel)
        if limit < 0:
            return False
        life = self.registryValue('%sLife' % kind,channel=channel)
        if not kind in chan.buffers:
            chan.buffers[kind] = {}
        if not key in chan.buffers[kind]:
            chan.buffers[kind][key] = utils.structures.TimeoutQueue(life)
        elif chan.buffers[kind][key].timeout != life:
            chan.buffers[kind][key].setTimeout(life)
        found = False
        for m in chan.buffers[kind][key]:
            if mask == m:
                found = True
                break
        if not found:
            chan.buffers[kind][key].enqueue(mask)
        i = self.getIrc(irc)
        if i.defcon:
            limit = limit - 1
            if limit < 0:
               limit = 0
        if len(chan.buffers[kind][key]) > limit:
            self.log.debug('abuse in %s : %s : %s/%s' % (channel,key,len(chan.buffers[kind][key]),limit))
            # chan.buffers[kind][key].reset()
            # queue not reseted, that way during life, it returns True
            if not chan.called:
                if not i.defcon:
                    self.logChannel(irc,"INFO: [%s] ignores lifted, limits lowered due to %s abuses for %ss" % (channel,key,self.registryValue('abuseDuration',channel=channel)))
                if not i.defcon:
                    i.defcon = time.time()
                    if not i.god:
                        irc.sendMsg(ircmsgs.IrcMsg('MODE %s +p' % irc.nick))
                    else:
                        self.applyDefcon(irc)
            chan.called = time.time()
            return True
        return False

    def isBadOnChannel (self,irc,channel,kind,key):
        chan = self.getChan(irc,channel)
        limit = self.registryValue('%sPermit' % kind,channel=channel)
        if limit < 0:
            return False
        i = self.getIrc(irc)
        if i.netsplit:
            kinds = ['flood','lowFlood','nick','lowRepeat','lowMassRepeat','broken']
            if kind in kinds:
                return False
        life = self.registryValue('%sLife' % kind,channel=channel)
        if limit == 0:
            return '%s %s/%ss in %s' % (kind,limit,life,channel)
        if not kind in chan.buffers:
            chan.buffers[kind] = {}
        newUser = False
        if not key in chan.buffers[kind]:
            newUser = True
            chan.buffers[kind][key] = utils.structures.TimeoutQueue(life)
            chan.buffers[kind]['%s-creation' % key] = time.time()
        elif chan.buffers[kind][key].timeout != life:
            chan.buffers[kind][key].setTimeout(life)
        ignore = self.registryValue('ignoreDuration',channel=channel)
        if ignore > 0:
           if time.time() - chan.buffers[kind]['%s-creation' % key] < ignore:
               newUser = True
        chan.buffers[kind][key].enqueue(key)
        if newUser or i.defcon or self.hasAbuseOnChannel(irc,channel,kind) or chan.called:
            limit = limit - 1
            if limit < 0:
                limit = 0
        if len(chan.buffers[kind][key]) > limit:
            chan.buffers[kind][key].reset()
            if not kind == 'broken':
                self.isAbuseOnChannel(irc,channel,kind,key)
            return '%s %s/%ss in %s' % (kind,limit,life,channel)
        return False

    def hasBadOnChannel (self,irc,channel,kind,key):
        chan = self.getChan(irc,channel)
        if not kind in chan.buffers:
            return False
        if not key in chan.buffers[kind]:
            return False;
        return len(chan.buffers[kind][key]) > 0

    def isChannelUniSpam (self,irc,msg,channel,mask,text):
        count = len([char for char in text if char in self.spamchars])
        return len(text) < 32 and count >=3

    def isChannelCtcp (self,irc,msg,channel,mask,text):
        return self.isBadOnChannel(irc,channel,'ctcp',mask)

    def isChannelNotice (self,irc,msg,channel,mask,text):
        return self.isBadOnChannel(irc,channel,'notice',mask)

    def isChannelLowFlood (self,irc,msg,channel,mask,text):
        return self.isBadOnChannel(irc,channel,'lowFlood',mask)

    def isChannelCap (self,irc,msg,channel,mask,text):
        text = text.replace(' ','')
        if len(text) == 0 or len(text) > self.registryValue('capMinimum',channel=channel):
            limit = self.registryValue('capPermit',channel=channel)
            if limit < 0:
                return False
            trigger = self.registryValue('capPercent',channel=channel)
            matchs = self.recaps.findall(text)
            #self.log.info ('%s : %s : %s :%s' % (mask,channel,text,len(matchs)))
            if len(matchs) and len(text):
                percent = (len(matchs)*100) / (len(text) * 1.0)
                #self.log.info ('%s: %s/%s %s' % (mask,percent,trigger,text))
                if percent >= trigger:
                    return self.isBadOnChannel(irc,channel,'cap',mask)
        return False

    def isChannelFlood (self,irc,msg,channel,mask,text):
        if len(text) == 0 or len(text) >= self.registryValue('floodMinimum',channel=channel) or text.isdigit():
            return self.isBadOnChannel(irc,channel,'flood',mask)
        return False

    def isChannelHilight (self,irc,msg,channel,mask,text):
        return self.isHilight(irc,msg,channel,mask,text,False)

    def isChannelLowHilight (self,irc,msg,channel,mask,text):
        return self.isHilight(irc,msg,channel,mask,text,True)

    def isChannelUnicode (self,irc,msg,channel,mask,text):
        limit = self.registryValue('badunicodeLimit',channel=channel)
        if limit > 0:
            score = sequence_weirdness(u'%s' % text)
            count = self.registryValue('badunicodeScore',channel=channel)
            if count < score:
                return self.isBadOnChannel(irc,channel,'badunicode',mask)
        return False

    def isHilight (self,irc,msg,channel,mask,text,low):
        kind = 'hilight'
        if low:
            kind = 'lowHilight'
        limit = self.registryValue('%sNick' % kind,channel=channel)
        if limit < 0:
            return False
        count = 0
        users = []
        if channel in irc.state.channels and irc.isChannel(channel):
            for u in list(irc.state.channels[channel].users):
                if u == 'ChanServ' or u == msg.nick:
                    continue
                users.append(u.lower())
        flag = False
        us = {}
        for user in users:
            if len(user) > 3:
                if not user in us and user in text:
                    us[user] = True
                    count = count + 1
                    if count > limit:
                        flag = True
                        break
        result = False
        if flag:
            result = self.isBadOnChannel(irc,channel,kind,mask)
        return result

    def isChannelRepeat (self,irc,msg,channel,mask,text):
        return self.isRepeat(irc,msg,channel,mask,text,False)

    def isChannelLowRepeat (self,irc,msg,channel,mask,text):
        return self.isRepeat(irc,msg,channel,mask,text,True)

    def isRepeat(self,irc,msg,channel,mask,text,low):
        kind = 'repeat'
        key = mask
        if low:
            kind = 'lowRepeat'
            key = 'low_repeat %s' % mask
        limit = self.registryValue('%sPermit' % kind,channel=channel)
        if limit < 0:
            return False
        if len(text) < self.registryValue('%sMinimum' % kind,channel=channel):
            return False
        chan = self.getChan(irc,channel)
        life = self.registryValue('%sLife'  % kind,channel=channel)
        trigger = self.registryValue('%sPercent' % kind,channel=channel)
        if not key in chan.logs:
            chan.logs[key] = utils.structures.TimeoutQueue(life)
        elif chan.logs[key].timeout != life:
            chan.logs[key].setTimeout(life)
        logs = chan.logs[key]
        flag = False
        result = False
        for m in logs:
            if compareString(m,text) > trigger:
                flag = True
                break
        if flag:
            result = self.isBadOnChannel(irc,channel,kind,mask)
        enough = False
        i = self.getIrc(irc)
        if flag and not i.netsplit:
            if kind in chan.buffers and key in chan.buffers[kind]:
                # we start to try to create pattern if user hits around 2/3 of his buffer
                if len(chan.buffers[kind][key])/(limit * 1.0) > 0.55:
                    enough = True
        if result or enough:
            life = self.registryValue('computedPatternLife',channel=channel)
            if not chan.patterns:
                chan.patterns = utils.structures.TimeoutQueue(life)
            elif chan.patterns.timeout != life:
                chan.patterns.setTimeout(life)
            if self.registryValue('computedPattern',channel=channel) > -1 and len(text) > self.registryValue('computedPattern',channel=channel):
                repeats = []
                if low:
                    pat = ''
                    for m in logs:
                        if compareString(m,text) > trigger:
                            p = largestString(m,text)
                            if len(p) > self.registryValue('computedPattern',channel=channel):
                                if len(p) > len(pat):
                                    pat = p
                    if len(pat):
                        repeats = [(pat,1)]
                else:
                    repeats = list(repetitions(text))
                candidate = ''
                patterns = {}
                for repeat in repeats:
                    (p,c) = repeat
                    #self.log.debug('%s :: %s' % (p,c))
                    if len(p) < self.registryValue('%sMinimum' % kind, channel=channel):
                        continue
                    p = p.strip()
                    if p in patterns:
                        patterns[p] += c
                    else:
                        patterns[p] = c
                    if len(p) > self.registryValue('computedPattern',channel=channel):
                        if len(p) > len(candidate):
                            candidate = p
                    elif len(p) * c > self.registryValue('computedPattern',channel=channel):
                        tentative = ''.join(list((p,) * int(c)))
                        if not tentative in text:
                            tentative = ''.join(list(((p + ' '),) * int(c)))
                            if not tentative in text:
                                tentative = ''
                        if len(tentative):
                            tentative = tentative[:self.registryValue('computedPattern',channel=channel)]
                        if len(tentative) > len(candidate):
                            candidate = tentative
                    elif patterns[p] > self.registryValue('%sCount' % kind,channel=channel):
                        if len(p) > len(candidate):
                            candidate = p
                if candidate.strip() == channel:
                    self.log.debug('pattern candidate %s discared in %s' % (candidate,channel))
                    candidate = ''
                if len(candidate) and len(candidate) > self.registryValue('%sMinimum' % kind, channel=channel):
                    found = False
                    for p in chan.patterns:
                        if p in candidate:
                            found = True
                            break
                    if not found:
                        candidate = candidate.strip()
                        shareID = self.registryValue('shareComputedPatternID',channel=channel)
                        i = self.getIrc(irc)
                        if shareID != -1 or i.defcon:
                            nb = 0
                            for chan in i.channels:
                                ch = i.channels[chan]
                                life = self.registryValue('computedPatternLife',channel=chan)
                                if shareID != self.registryValue('shareComputedPatternID',channel=chan):
                                    continue
                                if not ch.patterns:
                                    ch.patterns = utils.structures.TimeoutQueue(life)
                                elif ch.patterns.timeout != life:
                                    ch.patterns.setTimeout(life)
                                ch.patterns.enqueue(candidate)
                                nb = nb + 1
                            self.logChannel(irc,'PATTERN: [%s] %s added "%s" in %s channels (%s)' % (channel,mask,candidate,nb,kind))
                        else:
                            chan.patterns.enqueue(candidate)
                            self.logChannel(irc,'PATTERN: [%s] %s added "%s" for %ss (%s)' % (channel,mask,candidate,self.registryValue('computedPatternLife',channel=channel),kind))
        logs.enqueue(text)
        return result

    def isChannelMassRepeat (self,irc,msg,channel,mask,text):
        return self.isMassRepeat(irc,msg,channel,mask,text,False)

    def isChannelLowMassRepeat (self,irc,msg,channel,mask,text):
        return self.isMassRepeat(irc,msg,channel,mask,text,True)

    def isMassRepeat (self,irc,msg,channel,mask,text,low):
        kind = 'massRepeat'
        key = 'mass Repeat'
        if low:
            kind = 'lowMassRepeat'
            key = 'low mass Repeat'
        limit = self.registryValue('%sPermit' % kind,channel=channel)
        if limit < 0:
            return False
        if len(text) < self.registryValue('%sMinimum' % kind,channel=channel):
            return False
        chan = self.getChan(irc,channel)
        life = self.registryValue('%sLife' % kind,channel=channel)
        trigger = self.registryValue('%sPercent' % kind,channel=channel)
        length = self.registryValue('computedPattern',channel=channel)
        if not key in chan.logs:
            chan.logs[key] = utils.structures.TimeoutQueue(life)
        elif chan.logs[key].timeout != life:
            chan.logs[key].setTimeout(life)
        flag = False
        result = False
        pattern = None
        s = ''
        logs = chan.logs[key]
        for m in logs:
            found = compareString(m,text)
            if found > trigger:
                if length > 0:
                    pattern = largestString(m,text)
                    if len(pattern) < length:
                        pattern = None
                    else:
                        s = s.strip()
                        if len(s) > len(pattern):
                            pattern = s
                        s = pattern
                flag = True
                break
        if flag:
            result = self.isBadOnChannel(irc,channel,kind,channel)
            if result and pattern and length > -1:
                life = self.registryValue('computedPatternLife',channel=channel)
                if not chan.patterns:
                    chan.patterns = utils.structures.TimeoutQueue(life)
                elif chan.patterns.timeout != life:
                    chan.patterns.setTimeout(life)
                if len(pattern) > length:
                    pattern = pattern[:-1]
                    found = False
                    for p in chan.patterns:
                        if p in pattern:
                            found = True
                            break
                    if not found:
                        shareID = self.registryValue('shareComputedPatternID',channel=channel)
                        if shareID != -1:
                            nb = 0
                            i = self.getIrc(irc)
                            for chan in i.channels:
                                ch = i.channels[chan]
                                if shareID != self.registryValue('shareComputedPatternID',channel=chan):
                                    continue
                                life = self.registryValue('computedPatternLife',channel=chan)
                                if not ch.patterns:
                                    ch.patterns = utils.structures.TimeoutQueue(life)
                                elif ch.patterns.timeout != life:
                                    ch.patterns.setTimeout(life)
                                ch.patterns.enqueue(pattern)
                                nb = nb + 1
                            self.logChannel(irc,'PATTERN: [%s] %s added "%s" in %s channels (%s)' % (channel,mask,pattern,nb,kind))
                        else:
                            chan.patterns.enqueue(pattern)
                            self.logChannel(irc,'PATTERN: [%s] %s added "%s" for %ss (%s)' % (channel,mask,pattern,self.registryValue('computedPatternLife',channel=channel),kind))
        logs.enqueue(text)
        if result and pattern:
            return result
        return False

    def logChannel(self,irc,message):
        channel = self.registryValue('logChannel')
        i = self.getIrc(irc)
        if channel in irc.state.channels:
            self.log.info('logChannel : %s' % message)
            msg = ircmsgs.privmsg(channel,message)
            if self.registryValue('useNotice'):
                msg = ircmsgs.notice(channel,message)
            life = self.registryValue('announceLife')
            limit = self.registryValue('announcePermit')
            if limit > -1:
                q = self.getIrcQueueFor(irc,'status','announce',life)
                q.enqueue(message)
                if len(q) > limit:
                    if not i.throttled:
                        i.throttled = True
                        irc.queueMsg(ircmsgs.privmsg(channel,'NOTE: messages throttled to avoid spam for %ss' % life))
                        if not i.defcon:
                            self.logChannel(irc,"INFO: ignores lifted and abuses end to klines for %ss due to abuses" % self.registryValue('defcon'))
                            if not i.god:
                                irc.sendMsg(ircmsgs.IrcMsg('MODE %s +p' % irc.nick))
                            else:
                                for channel in irc.state.channels:
                                    if irc.isChannel(channel) and self.registryValue('defconMode',channel=channel):
                                        if not 'z' in irc.state.channels[channel].modes:
                                            if irc.nick in list(irc.state.channels[channel].ops):
                                                irc.sendMsg(ircmsgs.IrcMsg('MODE %s +qz $~a' % channel))
                                            else:
                                                irc.sendMsg(ircmsgs.IrcMsg('MODE %s +oqz %s $~a' % (channel,irc.nick)))

                        i.defcon = time.time()
                else:
                    i.throttled = False
                    if i.opered:
                        irc.sendMsg(msg)
                    else:
                        irc.queueMsg(msg)
            else:
                if i.opered:
                    irc.sendMsg(msg)
                else:
                    irc.queueMsg(msg)

    def doJoin (self,irc,msg):
        if irc.prefix == msg.prefix:
            i = self.getIrc(irc)
            return
        channels = msg.args[0].split(',')
        if not ircutils.isUserHostmask(msg.prefix):
            return
        if ircdb.checkCapability(msg.prefix, 'protected'):
            return
        i = self.getIrc(irc)
        prefix = msg.prefix
        gecos = None
        account = None
        if len(msg.args) == 3:
            gecos = msg.args[2]
            account = msg.args[1]
            if account == '*':
                account = None
            else:
                aa = account.lower()
                for u in i.klinednicks:
                    if aa == u:
                        self.logChannel(irc,"SERVICE: %s (%s) lethaled account (extended-join %s)" % (msg.prefix,account,msg.args[0]))
                        src = msg.nick
                        i.klinednicks.enqueue(aa)
                        if not src in i.tokline:
                            i.toklineresults[src] = {}
                            i.toklineresults[src]['kind'] = 'evade'
                            i.tokline[src] = src
                            def f ():
                                irc.sendMsg(ircmsgs.IrcMsg('WHOIS %s %s' % (src,src)))
                            schedule.addEvent(f,time.time()+random.randint(0,7))
                            #irc.sendMsg(ircmsgs.IrcMsg('WHOIS %s %s' % (src,src)))
                        break
        for channel in channels:
            if ircutils.isChannel(channel) and channel in irc.state.channels:
                if self.registryValue('ignoreChannel',channel):
                    continue
                chan = self.getChan(irc,channel)
                t = time.time()
                mask = self.prefixToMask(irc,msg.prefix,channel)
                if isCloaked(msg.prefix,self) or account:
                    t = t - self.registryValue('ignoreDuration',channel=channel) - 1
                chan.nicks[msg.nick] = [t,msg.prefix,mask,gecos,account]
                if self.registryValue('ignoreRegisteredUser',channel=channel):
                    if account:
                        continue
                if i.netsplit:
                    continue
                if 'gateway/shell/matrix.org' in msg.prefix:
                    continue
                life = self.registryValue('massJoinLife',channel=channel)
                limit = self.registryValue('massJoinPermit',channel=channel)
                trigger = self.registryValue('massJoinPercent',channel=channel)
                length = self.registryValue('massJoinMinimum',channel=channel)
                # massJoin for the whole channel
                flags = []
                if limit > -1:
                    b = self.isBadOnChannel(irc,channel,'massJoin',channel)
                    if b:
                        self.log.info('Massjoin detected in %s (%s/%s)' % (channel,life,limit))
                life = self.registryValue('massJoinHostLife',channel=channel)
                limit = self.registryValue('massJoinHostPermit',channel=channel)
                ## massJoin same ip/host
                if limit > -1:
                    b = self.isBadOnChannel(irc,channel,'massJoinHost',mask)
                    if b:
                        if not mask in flags:
                            flags.append(mask)
#                        self.logChannel(irc,'NOTE: [%s] %s (%s)' % (channel,b,mask))
#                life = self.registryValue('massJoinNickLife',channel=channel)
#                limit = self.registryValue('massJoinNickPermit',channel=channel)
                ## massJoin similar nicks
#                if limit > -1:
#                    key = 'massJoinNick'
#                    if not key in chan.logs:
#                        chan.logs[key] = utils.structures.TimeoutQueue(life)
#                    elif chan.logs[key].timeout != life:
#                        chan.logs[key].setTimeout(life)
#                    logs = chan.logs[key]
#                    flag = False
#                    pattern = ''
#                    for m in logs:
#                        if compareString(m,msg.nick) > trigger:
#                            flag = True
#                            p = largestString(m,msg.nick)
#                            if len(p) > len(pattern):
#                                pattern = p
#                    if flag and len(pattern) > length and not 'Guest' in pattern:
#                        b = self.isBadOnChannel(irc,channel,key,pattern)
#                        if b:
#                            if not mask in flags:
#                                flags.append(mask)
#                            self.logChannel(irc,'NOTE: [%s] %s (%s)' % (channel,b,pattern))
#                    logs.enqueue(msg.nick)
                ## massJoin similar gecos
#                life = self.registryValue('massJoinGecosLife',channel=channel)
#                limit = self.registryValue('massJoinGecosPermit',channel=channel)
#                if limit > -1:
#                    key = 'massJoinGecos'
#                    if not key in chan.logs:
#                        chan.logs[key] = utils.structures.TimeoutQueue(life)
#                    elif chan.logs[key].timeout != life:
#                        chan.logs[key].setTimeout(life)
#                    logs = chan.logs[key]
#                    flag = False
#                    pattern = ''
#                    for m in logs:
#                        if compareString(m,gecos) > trigger:
#                            flag = True
#                            p = largestString(m,gecos)
#                            if len(p) > len(pattern):
#                                pattern = p
#                    if flag and len(pattern) > length:
#                        b = self.isBadOnChannel(irc,channel,key,pattern)
#                        if b:
#                            if not mask in flags:
#                                flags.append(mask)
#                            self.logChannel(irc,'NOTE: [%s] %s (%s)' % (channel,b,pattern))
#                    logs.enqueue(gecos)
                if self.hasAbuseOnChannel(irc,channel,'cycle') and self.hasAbuseOnChannel(irc,channel,'massJoinHost') and len(flags) > 0 and self.registryValue('massJoinTakeAction',channel=channel):
                    for u in flags:
                        if not u in i.klines:
                            self.kill(irc,msg.nick,self.registryValue('killMessage',channel=channel))
                            uid = random.randint(0,1000000)
                            self.kline(irc,msg.prefix,u,self.registryValue('klineDuration'),'%s - cycle/massJoinHost %s !dnsbl' % (uid,channel))
                            self.logChannel(irc,'BAD: [%s] %s (cycle/massJoinHost %s - %s)' % (channel,u,msg.prefix,uid))

    def doPart (self,irc,msg):
        channels = msg.args[0].split(',')
        i = self.getIrc(irc)
        reason = ''
        if len(msg.args) == 2:
            reason = msg.args[1].lstrip().rstrip()
        if not ircutils.isUserHostmask(msg.prefix):
            return
        if msg.prefix == irc.prefix:
            for channel in channels:
                if ircutils.isChannel(channel):
                    self.setRegistryValue('lastActionTaken',time.time(),channel=channel)
                    self.logChannel(irc,'PART: [%s] %s' % (channel,reason))
                    if channel in i.channels:
                        del i.channels[channel]
            return
        mask = self.prefixToMask(irc,msg.prefix)
        isBanned = False
        reason = ''
        if len(msg.args) == 2:
            reason = msg.args[1].lstrip().rstrip()
        for channel in channels:
            if ircutils.isChannel(channel) and channel in irc.state.channels and not isBanned:
                chan = self.getChan(irc,channel)
                if msg.nick in chan.nicks:
                    if self.registryValue('ignoreChannel',channel):
                        continue
                    if self.registryValue('ignoreRegisteredUser',channel=channel):
                        if len(chan.nicks[msg.nick]) > 4:
                            if chan.nicks[msg.nick][4]:
                                continue
                    protected = ircdb.makeChannelCapability(channel, 'protected')
                    if ircdb.checkCapability(msg.prefix, protected):
                        continue
                    if reason == 'Changing Host' or i.netsplit:
                        continue
                    bad = False
                    if len(reason) and 'Kicked by @appservice-irc:matrix.org' in reason:
                        continue
                    flag = ircdb.makeChannelCapability(channel, 'cycle')
                    if ircdb.checkCapability(msg.prefix, flag):
                        bad = self.isBadOnChannel(irc,channel,'cycle',mask)
                        self.isAbuseOnChannel(irc,channel,'cycle',mask)
                    if bad:
                        isBanned = True
                        uid = random.randint(0,1000000)
                        log = "BAD: [%s] %s (join/part - %s)" % (channel,msg.prefix,uid)
                        comment = '%s - join/part flood in %s' % (uid,channel)
                        self.ban(irc,msg.nick,msg.prefix,mask,self.registryValue('klineDuration'),comment,self.registryValue('klineMessage'),log)
                        self.setRegistryValue('lastActionTaken',time.time(),channel=channel)
                    if len(reason):
                        if 'Kicked by @appservice-irc:matrix.org' in reason or 'requested by' in reason:
                            continue
                        bad = self.isChannelMassRepeat(irc,msg,channel,mask,reason)
                        if bad:
                            # todo, needs to see more on that one to avoid false positive
                            #self.kill(irc,msg.nick,msg.prefix)
                            #self.kline(irc,msg.prefix,mask,self.registryValue('klineDuration'),'%s in %s' % (bad,channel))
                            self.logChannel(irc,"IGNORED: [%s] %s (Part's message %s) : %s" % (channel,msg.prefix,bad,reason))
                    if not isBanned:
                        life = self.registryValue('abuseDuration',channel=channel)
                        if self.hasAbuseOnChannel(irc,channel,'cycle') and time.time() - chan.nicks[msg.nick][0] < life:
                            isBanned = True
                            uid = random.randint(0,1000000)
                            log = "BAD: [%s] %s (cycle abuse - %s)" % (channel,msg.prefix,uid)
                            comment = '%s - cycle abuse in %s' % (uid,channel)
                            self.ban(irc,msg.nick,msg.prefix,mask,self.registryValue('klineDuration'),comment,self.registryValue('klineMessage'),log)
                            self.setRegistryValue('lastActionTaken',time.time(),channel=channel)
                        flag = ircdb.makeChannelCapability(channel, 'joinSpamPart')
                        if ircdb.checkCapability(msg.prefix, flag) and not isBanned:
                            limit = self.registryValue('joinSpamPartPermit',channel=channel)
                            if limit > -1:
                                kind = 'joinSpamPart'
                                life = self.registryValue('joinSpamPartLife',channel=channel)
                                key = mask
                                if kind in chan.buffers and key in chan.buffers[kind] and len(chan.buffers[kind][key]) == limit and msg.nick in chan.nicks and time.time() - chan.nicks[msg.nick][0] < life:
                                    self.isAbuseOnChannel(irc,channel,'joinSpamPart',mask)
                                    if self.hasAbuseOnChannel(irc,channel,'joinSpamPart'):
                                        uid = random.randint(0,1000000)
                                        reason = '(%s/%ss joinSpamPart)' % (limit,life)
                                        klinereason = '%s - %s' % (uid,reason)
                                        if i.defcon:
                                            klinereason = '%s !dnsbl' % reason
                                        self.kline(irc,msg.prefix,mask,self.registryValue('klineDuration'),klinereason)
                                        self.logChannel(irc,'BAD: [%s] %s (%s - %s)'  (channel,msg.prefix,reason,uid))
                                        isBanned = True
                                        chan.buffers[kind][key].reset()
                                        continue
    def doKick (self,irc,msg):
        channel = target = reason = None
        if len(msg.args) == 3:
            (channel,target,reason) = msg.args
        else:
            (channel,target) = msg.args
            reason = ''
        i = self.getIrc(irc)
        if target == irc.nick:
            if channel in i.channels:
                self.setRegistryValue('lastActionTaken',-1.0,channel=channel)
                self.logChannel(irc,'PART: [%s] %s (kicked)' % (channel,reason))
                del i.channels[channel]
                try:
                    network = conf.supybot.networks.get(irc.network)
                    network.channels().remove(channel)
                except KeyError:
                    pass

    def doQuit (self,irc,msg):
        if msg.prefix == irc.prefix:
            return
        reason = ''
        if len(msg.args) == 1:
            reason = msg.args[0].lstrip().rstrip()
        i = self.getIrc(irc)
        if reason == '*.net *.split':
            if not i.netsplit:
                self.logChannel(irc,'INFO: netsplit activated for %ss : some abuses are ignored' % self.registryValue('netsplitDuration'))
            i.netsplit = time.time() + self.registryValue('netsplitDuration')
        if i.netsplit:
            return
        mask = self.prefixToMask(irc,msg.prefix)
        isBanned = False
        (nick,ident,host) = ircutils.splitHostmask(msg.prefix)
        for channel in irc.state.channels:
            if ircutils.isChannel(channel) and not i.netsplit:
               chan = self.getChan(irc,channel)
               if self.registryValue('ignoreChannel',channel):
                   continue
               if msg.nick in chan.nicks:
                    if self.registryValue('ignoreRegisteredUser',channel=channel):
                        if len(chan.nicks[msg.nick]) > 4:
                            if chan.nicks[msg.nick][4]:
                                continue
                    protected = ircdb.makeChannelCapability(channel, 'protected')
                    if ircdb.checkCapability(msg.prefix, protected):
                        continue
                    bad = False
                    flag = ircdb.makeChannelCapability(channel, 'broken')
                    if 'tor-sasl' in mask:
                        continue
                    if ircdb.checkCapability(msg.prefix, flag):
                        bad = self.isBadOnChannel(irc,channel,'broken',mask)
                    if isBanned:
                        continue
                    if bad and not i.netsplit:
                        uid = random.randint(0,1000000)
                        self.kline(irc,msg.prefix,mask,self.registryValue('brokenDuration'),'%s - %s in %s' % (uid,'join/quit flood',channel),self.registryValue('brokenReason') % self.registryValue('brokenDuration'))
                        self.logChannel(irc,'BAD: [%s] %s (%s - %s)' % (channel,msg.prefix,'broken client',uid))
                        isBanned = True
                        continue
                    flag = ircdb.makeChannelCapability(channel, 'joinSpamPart')
                    if ircdb.checkCapability(msg.prefix, flag) and reason == 'Remote host closed the connection':
                        limit = self.registryValue('joinSpamPartPermit',channel=channel)
                        if limit > -1:
                            kind = 'joinSpamPart'
                            life = self.registryValue('joinSpamPartLife',channel=channel)
                            key = mask
                            if kind in chan.buffers and key in chan.buffers[kind] and len(chan.buffers[kind][key]) == limit and msg.nick in chan.nicks and time.time() - chan.nicks[msg.nick][0] < life:
                                self.isAbuseOnChannel(irc,channel,'joinSpamPart',mask)
                                if self.hasAbuseOnChannel(irc,channel,'joinSpamPart'):
                                    uid = random.randint(0,1000000)
                                    reason = '(%s/%ss joinSpamPart)' % (limit,life)
                                    klinereason = '%s - %s' % (uid,reason)
                                    if i.defcon:
                                        klinereason = '%s !dnsbl' % reason
                                    self.kline(irc,msg.prefix,mask,self.registryValue('klineDuration'),klinereason)
                                    self.logChannel(irc,'BAD: [%s] %s (%s - %s)' % (channel,msg.prefix,reason,uid))
                                    isBanned = True
                                    chan.buffers[kind][key].reset()
                                    continue
                    hosts = self.registryValue('brokenHost',channel=channel)
                    reasons = ['Read error: Connection reset by peer','Client Quit','Excess Flood','Max SendQ exceeded','Remote host closed the connection']
                    if 'broken' in chan.buffers and mask in chan.buffers['broken'] and len(chan.buffers['broken'][mask]) > 1 and reason in reasons and len(hosts):
                        found = False
                        for h in hosts:
                            if len(h):
                                if h.isdigit() and host.startswith(h):
                                    found = True
                                    break
                                if h in host:
                                    found = True
                                    break
                        if found and len(chan.nicks[msg.nick]) == 5:
                            gecos = chan.nicks[msg.nick][3]
                            account = chan.nicks[msg.nick][4]
                            if not account and gecos == msg.nick and gecos in ident and len(msg.nick) < 6:
                                isBanned = True
                                uid = random.randint(0,1000000)
                                self.kline(irc,msg.prefix,mask,self.registryValue('brokenDuration')*4,'%s - %s in %s' % (uid,'join/quit flood',channel),self.registryValue('brokenReason') % (self.registryValue('brokenDuration')*4))
                                self.logChannel(irc,'BAD: [%s] %s (%s - %s)' % (channel,msg.prefix,'broken bottish client',uid))

    def doNick (self,irc,msg):
        oldNick = msg.prefix.split('!')[0]
        newNick = msg.args[0]
        if oldNick == irc.nick or newNick == irc.nick:
            return
        newPrefix = '%s!%s' % (newNick,msg.prefix.split('!')[1])
        mask = self.prefixToMask(irc,newPrefix)
        i = self.getIrc(irc)
        if i.netsplit:
            return
        isBanned = False
        for channel in irc.state.channels:
            if ircutils.isChannel(channel):
                if self.registryValue('ignoreChannel',channel):
                    continue
                protected = ircdb.makeChannelCapability(channel, 'protected')
                if ircdb.checkCapability(newPrefix, protected):
                    continue
                chan = self.getChan(irc,channel)
                if oldNick in chan.nicks:
                    chan.nicks[newNick] = chan.nicks[oldNick]
                    # todo check digit/hexa nicks too
                    if not newNick.startswith('Guest'):
                        if not isBanned:
                            reason = False
                            if self.registryValue('ignoreRegisteredUser',channel=channel):
                                if newNick in chan.nicks and len(chan.nicks[newNick]) > 4 and chan.nicks[newNick][4]:
                                    continue
                            flag = ircdb.makeChannelCapability(channel, 'nick')
                            if ircdb.checkCapability(msg.prefix, flag):
                                reason = self.isBadOnChannel(irc,channel,'nick',mask)
                            hasBeenIgnored = False
                            ignore = self.registryValue('ignoreDuration',channel=channel)
                            if ignore > 0:
                                ts = chan.nicks[newNick][0]
                                if time.time()-ts > ignore:
                                    hasBeenIgnored = True
                            if not isCloaked(msg.prefix,self):
                                if i.defcon or chan.called:
                                    hasBeenIgnored = False
                            if not reason and i.defcon and self.hasAbuseOnChannel(irc,channel,'nick'):
                                reason = 'nick changes, due to abuses'
                            if reason:
                                if hasBeenIgnored:
                                    bypass = self.isBadOnChannel(irc,channel,'bypassIgnore',mask)
                                    if bypass:
                                        uid = random.randint(0,1000000)
                                        comment = '%s %s' % (reason,bypass)
                                        log = 'BAD: [%s] %s (%s - %s)' % (channel,newPrefix,comment,uid)
                                        self.ban(irc,newNick,newPrefix,mask,self.registryValue('klineDuration'),'%s - %s' % (uid,comment),self.registryValue('klineMessage'),log)
                                        self.setRegistryValue('lastActionTaken',time.time(),channel=channel)
                                        isBanned = True
                                    else:
                                        self.logChannel(irc,'IGNORED: [%s] %s (%s)' % (channel,newPrefix,reason))
                                else:
                                    uid = random.randint(0,1000000)
                                    log = 'BAD: [%s] %s (%s - %s)' % (channel,newPrefix,reason,uid)
                                    self.ban(irc,newNick,newPrefix,mask,self.registryValue('klineDuration'),'%s - %s' % (uid,reason),self.registryValue('klineMessage'),log)
                                    self.setRegistryValue('lastActionTaken',time.time(),channel=channel)
                                    isBanned = True
                    del chan.nicks[oldNick]

    def reset(self):
        self._ircs = ircutils.IrcDict()

    def die(self):
        self.log.info('die() called')
        self.cache = {}
        try:
            conf.supybot.protocols.irc.throttleTime.setValue(1.6)
        except:
            pass
        self._ircs = ircutils.IrcDict()
        super().die()

    def doError (self,irc,msg):
        self._ircs = ircutils.IrcDict()

    def makeDb(self, filename):
        """Create a database and connect to it."""
        if os.path.exists(filename):
            db = sqlite3.connect(filename,timeout=10)
            db.text_factory = str
            return db
        db = sqlite3.connect(filename)
        db.text_factory = str
        c = db.cursor()
        c.execute("""CREATE TABLE patterns (
                id INTEGER PRIMARY KEY,
                pattern VARCHAR(512) NOT NULL,
                regexp INTEGER,
                mini INTEGER,
                life INTEGER,
                operator VARCHAR(512) NOT NULL,
                comment VARCHAR(512),
                triggered INTEGER,
                at TIMESTAMP NOT NULL,
                removed_at TIMESTAMP,
                removed_by VARCHAR(512)
                )""")
        db.commit()
        c.close()
        return db

    def getDb(self, irc):
        """Use this to get a database for a specific irc."""
        currentThread = threading.currentThread()
        if irc not in self.dbCache and currentThread == world.mainThread:
            self.dbCache[irc] = self.makeDb(self.makeFilename(irc))
        if currentThread != world.mainThread:
            db = self.makeDb(self.makeFilename(irc))
        else:
            db = self.dbCache[irc]
        db.isolation_level = None
        return db

Class = Sigyn

