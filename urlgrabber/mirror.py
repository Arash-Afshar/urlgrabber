# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Library General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA 02111-1307, USA.

# Copyright 2002-2004 Michael D. Stenner, Ryan Tomayko

"""Module for downloading files from a pool of mirrors

DESCRIPTION

  This module provides support for downloading files from a pool of
  mirrors with configurable failover policies.  To a large extent, the
  failover policy is chosen by using different classes derived from
  the main class, MirrorGroup.

  Instances of MirrorGroup (and cousins) act very much like URLGrabber
  instances in that they have urlread, urlgrab, and urlopen methods.
  They can therefore, be used in very similar ways.

    from urlgrabber.grabber import URLGrabber
    from urlgrabber.mirror import MirrorGroup
    gr = URLGrabber()
    mg = MirrorGroup(gr, ['http://foo.com/some/directory/',
                          'http://bar.org/maybe/somewhere/else/',
                          'ftp://baz.net/some/other/place/entirely/']
    mg.urlgrab('relative/path.zip')

  The assumption is that all mirrors are identical AFTER the base urls
  specified, so that any mirror can be used to fetch any file.

FAILOVER

  The failover mechanism is designed to be customized by subclassing
  from MirrorGroup to change the details of the behavior.  In general,
  the classes maintain a master mirror list and a "current mirror"
  index.  When a download is initiated, a copy of this list and index
  is created for that download only.  The specific failover policy
  depends ont he class used, and so is documented in the class
  documentation.  Note that ANY behavior of the class can be
  overridden, so any failover policy at all is possible (although
  you may need to change the interface in extreme cases).

CUSTOMIZATION

  Most customization of a MirrorGroup object is done at instantiation
  time (or via subclassing).  There are four major types of
  customization:

    1) Pass in a custom urlgrabber - The passed in urlgrabber will be
       used (by default... see #2) for the grabs, so options to it
       apply for the url-fetching

    2) Custom mirror list - Mirror lists can simply be a list of
       stings mirrors (as shown in the example above) but each can
       also be a dict, allowing for more options.  For example, the
       first mirror in the list above could also have been:

         {'mirror': 'http://foo.com/some/directory/',
          'grabber': <a custom grabber to be used for this mirror>,
          'kwargs': { <a dict of arguments passed to the grabber> }}

       All mirrors are converted to this format internally.  If
       'grabber' is omitted, the default grabber will be used.  If
       kwargs are omitted, then (duh) they will not be used.

    3) Pass keyword arguments when instantiating the mirror group.
       See, for example, the failure_callback argument.

    4) Finally, any kwargs passed in for the specific file (to the
       urlgrab method, for example) will be folded in.  The options
       passed into the grabber's urlXXX methods will override any
       options specified in a custom mirror dict.

"""
import random
import thread  # needed for locking to make this threadsafe

from urlgrabber.grabber import URLGrabError

DEBUG=0

try:
    from i18n import _
except ImportError, msg:
    def _(st): return st

class GrabRequest:
    """This is a dummy class used to hold information about the specific
    request.  For example, a single file.  By maintaining this information
    separately, we can accomplish two things:

      1) make it a little easier to be threadsafe
      2) have request-specific parameters
    """
    pass

class MirrorGroup:
    """Base Mirror class

    Instances of this class are built with a grabber object and a list
    of mirrors.  Then all calls to urlXXX should be passed relative urls.
    The requested file will be searched for on the first mirror.  If the
    grabber raises an exception (possibly after some retries) then that
    mirror will be removed from the list, and the next will be attempted.
    If all mirrors are exhausted, then an exception will be raised.

    MirrorGroup has the following failover policy:

      * downloads begin with the first mirror

      * a failure (after retries) causes it to increment the local AND
        master indices.  Also, the current mirror is removed from the
        local list (but NOT the master list - the mirror can potentially
        be used for other files)

      * if the local list is ever exhausted, an URLGrabError will be
        raised (errno=256, no more mirrors)

    OPTIONS

      In addition to the required arguments "grabber" and "mirrors",
      MirrorGroup also takes the following optional arguments:
      
      failure_callback

        this is a callback that will be called when a mirror "fails",
        meaning the grabber raises some URLGrabError.  If this is a
        tuple, it is enterpreted to be of the form (cb, args, kwargs)
        where cb is the actual callable object (function, method,
        etc).  Otherwise, it is assumed to be the callable object
        itself.  The callback will be passed exception that was raised
        (and args and kwargs if present).

        If the callback returns an integer, it will set the "remove
        policy" for that failure:

          0:  do not remove the mirror - simply move on
          1:  remove the mirror for this download attempt (this file)
          2:  remove the mirror for all future downloads

        WARNING: do not save the exception object.  As they contain
        stack frame references, they can lead to circular references.

    Notes:
      * The behavior can be customized by deriving and overriding the
        'CONFIGURATION METHODS'
      * The 'grabber' instance is kept as a reference, not copied.
        Therefore, the grabber instance can be modified externally
        and changes will take effect immediately.
    """

    # notes on thread-safety:

    #   A GrabRequest should never be shared by multiple threads because
    #   it's never saved inside the MG object and never returned outside it.
    #   therefore, it should be safe to access/modify grabrequest data
    #   without a lock.  However, accessing the mirrors and _next attributes
    #   of the MG itself must be done when locked to prevent (for example)
    #   removal of the wrong mirror.

    ##############################################################
    #  CONFIGURATION METHODS  -  intended to be overrident to
    #                            customize behavior
    def __init__(self, grabber, mirrors, **kwargs):
        """Initialize the MirrorGroup object.

        REQUIRED ARGUMENTS

          grabber  - URLGrabber instance
          mirrors  - a list of mirrors

        OPTIONAL ARGUMENTS

          failure_callback  - callback to be used when a mirror fails

        See the module-level and class level documentation for more
        details.
        """

        # OVERRIDE IDEAS:
        #   shuffle the list to randomize order
        self.grabber = grabber
        self.mirrors = self._parse_mirrors(mirrors)
        self._next = 0
        self._lock = thread.allocate_lock()
        self._process_kwargs(kwargs)

    def _process_kwargs(self, kwargs):
        self.failure_callback = kwargs.get('failure_callback')
        
    def _parse_mirrors(self, mirrors):
        parsed_mirrors = []
        for m in mirrors:
            if type(m) == type(''): m = {'mirror': m}
            parsed_mirrors.append(m)
        return parsed_mirrors
    
    def _load_gr(self, gr):
        # OVERRIDE IDEAS:
        #   shuffle gr list
        self._lock.acquire()
        gr.mirrors = list(self.mirrors)
        gr._next = self._next
        self._lock.release()

    def _get_mirror(self, gr):
        # OVERRIDE IDEAS:
        #   return a random mirror so that multiple mirrors get used
        #   even without failures.
        if not gr.mirrors:
            raise URLGrabError(256, _('No more mirrors to try.'))
        return gr.mirrors[gr._next]

    def _failure(self, gr, e):
        # OVERRIDE IDEAS:
        #   inspect the error - remove=1 for 404, remove=2 for connection
        #                       refused, etc. (this can also be done via
        #                       the callback)
        cb = self.failure_callback
        if cb is None:
            remove = 1
        else:
            if type(cb) == type( () ):
                cb, args, kwargs = cb
            else:
                args, kwargs = (), {}
            remove = cb(e, *args, **kwargs)
            if remove is None: remove = 1
        self.increment_mirror(gr, remove=1)

    def increment_mirror(self, gr, remove=1):
        """Tell the mirror object increment the mirror index

        This increments the mirror index, which amounts to telling the
        mirror object to use a different mirror (for this and future
        downloads).

        This is a SEMI-public method.  It will be called internally,
        and you may never need to call it.  However, it is provided
        (and is made public) so that the calling program can increment
        the mirror choice for methods like urlopen.  For example, with
        urlopen, there's no good way for the mirror group to know that
        an error occurs mid-download (it's already returned and given
        you the file object).
        
        remove  ---  can have several values
           0   do not remove the mirror from the list
           1   remove the mirror for this download only
           2   remove the mirror permanently

        beware of remove=0 as it can lead to infinite loops
        """
        badmirror = gr.mirrors[gr._next]

        self._lock.acquire()
        try:
            ind = self.mirrors.index(badmirror)
        except ValueError:
            pass
        else:
            if remove == 2: del self.mirrors[ind]
            if self._next == ind: self._next += 1
            if self._next >= len(self.mirrors): self._next = 0
        self._lock.release()
        
        if remove: del gr.mirrors[gr._next]
        gr._next += 1
        if gr._next >= len(gr.mirrors): gr._next = 0

        if DEBUG:
            grm = [m['mirror'] for m in gr.mirrors]
            print 'GR   mirrors: [%s] %i' % (' '.join(grm), gr._next)
            selfm = [m['mirror'] for m in self.mirrors]
            print 'MAIN mirrors: [%s] %i' % (' '.join(selfm), self._next)

    #####################################################################
    # NON-CONFIGURATION METHODS
    # these methods are designed to be largely workhorse methods that
    # are not intended to be overridden.  That doesn't mean you can't;
    # if you want to, feel free, but most things can be done by
    # by overriding the configuration methods :)

    def _join_url(self, base_url, rel_url):
        if base_url.endswith('/') or rel_url.startswith('/'):
            return base_url + rel_url
        else:
            return baseurl + '/' + rel_url
        
    def _mirror_try(self, func, url, kw):
        gr = GrabRequest()
        gr.func = func
        gr.url  = url
        gr.kw   = kw
        self._load_gr(gr)

        while 1:
            mirrorchoice = self._get_mirror(gr)
            fullurl = self._join_url(mirrorchoice['mirror'], gr.url)
            kwargs = dict(mirrorchoice.get('kwargs', {}))
            kwargs.update(kw)
            grabber = mirrorchoice.get('grabber') or self.grabber
            func_ref = getattr(grabber, func)
            if DEBUG: print 'MIRROR: trying %s -> %s' % (url, fullurl)
            try:
                return func_ref( *(fullurl,), **kwargs )
            except URLGrabError, e:
                if DEBUG: print 'MIRROR: failed'
                self._failure(gr, e)

    def urlgrab(self, url, filename=None, **kwargs):
        kw = dict(kwargs)
        kw['filename'] = filename
        func = 'urlgrab'
        return self._mirror_try(func, url, kw)
    
    def urlopen(self, url, **kwargs):
        kw = dict(kwargs)
        func = 'urlopen'
        return self._mirror_try(func, url, kw)

    def urlread(self, url, limit=None, **kwargs):
        kw = dict(kwargs)
        kw['limit'] = limit
        func = 'urlread'
        return self._mirror_try(func, url, kw)
            

class MGRandomStart(MirrorGroup):
    """A mirror group that starts at a random mirror in the list.

    This behavior of this class is identical to MirrorGroup, except that
    it starts at a random location in the mirror list.
    """

    def __init__(self, grabber, mirrors, **kwargs):
        """Initialize the object

        The arguments for intialization are the same as for MirrorGroup
        """
        MirrorGroup.__init__(self, grabber, mirrors, **kwargs)
        self._next = random.randrange(len(mirrors))

class MGRandomOrder(MirrorGroup):
    """A mirror group that uses mirrors in a random order.

    This behavior of this class is identical to MirrorGroup, except that
    it uses the mirrors in a random order.  Note that the order is set at
    initialization time and fixed thereafter.  That is, it does not pick a
    random mirror after each failure.
    """

    def __init__(self, grabber, mirrors, **kwargs):
        """Initialize the object

        The arguments for intialization are the same as for MirrorGroup
        """
        MirrorGroup.__init__(self, grabber, mirrors, **kwargs)
        random.shuffle(self.mirrors)

if __name__ == '__main__':
    pass
