"""
A high level session object to the lower level irclib.connection module.
"""
from __future__ import unicode_literals
from __future__ import print_function
from __future__ import absolute_import

from . import utils
from . import connection
from . import dcc

from . import logger
logger = logger.getChild('session')

import time
import select
import bisect
import collections
import re

# TODO: move this somewhere else
DEBUG = 0



#: All the high level events that we can register to.
#: Low level events that aren't on this list can be registered to as well,
#: but they will not be parsed.
high_level_events = ['connect',
                     'text',
                     'join',
                     'part',
                     'kick',
                     'quit',
                     'mode',
                     'umode',
                     'topic',
                     'invite',
                     'ctcp',
                     'ctcpreply'
                     'action',
                     'nick',
                     'raw'
                     ]

class Session:
    """Class that handles one or several IRC server connections.

    When a Session object has been instantiated, it can be used to create
    Connection objects that represent the IRC connections.  The
    responsibility of the Session object is to provide a high-level
    event-driven framework for the connections and to keep the connections
    alive. It runs a select loop to poll each connection's TCP socket and
    hands over the sockets with incoming data for processing by the
    corresponding connection. It then encapsulates the low level IRC
    events generated by the Connection objects into higher level
    versions.
    """

    def __init__(self, encoding='utf-8', handle_ctcp=True):
        """Constructor for :class:`Session` objects.
        
            :param encoding: The encoding that we should treat the incoming
                              data as.
            
            :param handle_ctcp: If this is True, the Session will respond to
                                 common CTCP commands like VERSION and PING
                                 on its own. It will still generate high level
                                 events.
        
        See :meth:`process_once` for information on how to run the Session
        object.
        """

        self.connections = []        
        self.delayed_commands = [] # list of tuples in the format (time, function, arguments)
        self.encoding = encoding
        self.handle_ctcp = handle_ctcp
        
        # CTCP response values
        #: Used to respond to CTCP VERSION messages.
        self.ctcp_version = "Hanyuu IRC Lib 1.3"
        #: Used to respond to CTCP SOURCE messages.
        self.ctcp_source = "https://github.com/R-a-dio/Hanyuu-sama/"
        
    def server(self):
        """Creates and returns a :class:`connection.ServerConnection` object."""

        c = connection.ServerConnection(self)
        self.connections.append(c)
        return c

    def process_data(self, sockets):
        """Called when there is more data to read on connection sockets.

            :param sockets: A list of socket objects to be processed.

        .. seealso: :meth:`process_once`
        """
        for s in sockets:
            for c in self.connections:
                if s == c._get_socket():
                    c.process_data()

    def process_timeout(self):
        """This is called to process any delayed commands that are registered
        to the Session object.
        
        .. seealso:: :meth:`process_once`
        """
        t = time.time()
        while self.delayed_commands:
            if t >= self.delayed_commands[0][0]:
                self.delayed_commands[0][1](*self.delayed_commands[0][2])
                del self.delayed_commands[0]
            else:
                break

    def send_once(self):
        """This method will send data to the servers from the message queue
        at a limited rate. The default is 2500 bytes per 1.3 seconds. This
        value cannot currently be changed.
        
        """
        
        for c in self.connections:
            try:
                delta = time.time() - c.last_time
            except (AttributeError):
                continue
            c.last_time = time.time()
            c.send_time += delta
            if c.send_time >= 1.3:
                c.send_time = 0
                c.sent_bytes = 0
            
            while not c.message_queue.empty():
                if c.sent_bytes <= 2500:
                    message = c.message_queue.get()
                    try:
                        if c.ssl:
                            c.send_raw_instant(message)
                        else:
                            c.send_raw_instant(message)
                    except (AttributeError):
                        c.reconnect()
                    c.sent_bytes += len(message.encode('utf-8'))
                    if DEBUG:
                        logger.debug("TO SERVER:" + message)
                else:
                    break

    def process_once(self, timeout=0):
        """Process data from connections once.
        
            :param timeout: How long the select() call should wait if no
                             data is available.

        This method should be called periodically to check and process
        incoming and outgoing data, if there is any.
        
        It calls :meth:`process_data`, :meth:`send_once` and
        :meth:`process_timeout`.
        
        It will also examine when we last received data from the server; if it
        exceeds a specified time limit, the Session assumes that we have lost
        connection to the server and will attempt to reconnect us.
        
        If that seems boring, look at the :meth:`process_forever` method.
        """
        sockets = map(lambda x: x._get_socket(), self.connections)
        sockets = filter(lambda x: x != None, sockets)
        if sockets:
            (i, o, e) = select.select(sockets, [], [], timeout)
            # Process incoming data
            self.process_data(i)
        else:
            time.sleep(timeout)
        _current_time = time.time()
        for connection in self.connections:
            try:
                _difference = _current_time - connection._last_ping
            except (AttributeError):
                continue
            if (_difference >= 260.0):
                logger.info("No data in the past 260 seconds, disconnect")
                connection.reconnect("Ping timeout: 260 seconds")
        # Send outgoing data
        self.send_once()
        # Check delayed calls
        self.process_timeout()
        
    def process_forever(self, timeout=0.2):
        """Run an infinite loop, processing data from connections.

        This method repeatedly calls :meth:`process_once`.
            
            :param timeout: Parameter to pass to process_once.
        """
        while 1:
            self.process_once(timeout)

    def disconnect_all(self, message=""):
        """Disconnects all connections.
            
            :param message: The quit message to send to servers.
        """
        for c in self.connections:
            c.disconnect(message)

    def execute_at(self, at, function, arguments=()):
        """Execute a function at a specified time.

            :param at: Time to execute at (standard \"time_t\" time).

            :param function: The function to call.

            :param arguments: Arguments to give the function.
        """
        self.execute_delayed(at-time.time(), function, arguments)

    def execute_delayed(self, delay, function, arguments=()):
        """Execute a function after a specified time.

            :param delay: How many seconds to wait.

            :param function: The function to call.

            :param arguments: Arguments to give the function.
        """
        bisect.insort(self.delayed_commands,
                      (delay+time.time(), function, arguments))

    def dcc(self, dcctype="chat", dccinfo=(None, 0)):
        """Creates and returns a :class:`connection.DCCConnection` object.

            :param dcctype: "chat" for DCC CHAT connections or "raw" for
                             DCC SEND (or other DCC types). If "chat",
                             incoming data will be split in newline-separated
                             chunks. If "raw", incoming data is not touched.
        """
        c = dcc.DCCConnection(self, dcctype, dccinfo)
        self.connections.append(c)
        return c

    def _handle_event(self, server, event):
        """Internal event handler.
        
        Receives events from :class:`connection.ServerConnection` and converts
        them into high level events, then dispatches them to event handlers.
        """
        
        # PONG any incoming PING event
        if event.eventtype == 'ping':
            self._ping_ponger(server, event)
        
        # Should we handle the common CTCP events?
        if self.handle_ctcp and event.eventtype == 'ctcp':
            try:
                self._ctcp_handler(server, event)
            except:
                logger.exception('Error in CTCP handler')
        
        # Preparse MODE events, we want them separate in high level
        if event.eventtype in ['mode', 'umode']:
            modes = server._parse_modes(' '.join(event.argument))
            # do we have more than 1 mode? split and rehandle
            if len(modes) > 1:
                for sign, mode, param in modes:
                    # if the parameter is empty, make it blank
                    # otherwise the joining breaks later on
                    if not param:
                        param = ''
                    new_event = connection.Event(event.eventtype,
                                                 event.source,
                                                 event.target,
                                                 [sign+mode, param])
                    # Reraise the individual events as low level
                    self._handle_event(server, new_event)
                # If we had to preparse, end here
                return
        
        # Rebuild the low level event into a high level one
        high_event = HighEvent.from_low_event(server, event)
        
        handlers = Session.handlers
        
        command = high_event.command
        channel = high_event.channel
        nickname = high_event.nickname
        message = high_event.message
        
        if channel:
            channel = channel.lower()
        if nickname:
            nickname = nickname.name.lower()

        for function, events, channels, nicks, modes, regex in handlers.values():
            # command is guaranteed to exist, no need to do .lower in advance
            if events and command.lower() not in events:
                continue
            if channels and channel not in channels:
                continue
            if nicks and nickname not in nicks:
                continue
            if channel and nickname and modes != '':
                # If the triggering nick does not have any of the needed modes
                if not server.hasanymodes(channel,
                                          nickname,
                                          modes):
                    # Don't trigger the handler
                    continue
            if message and regex:
                if not regex.match(message):
                    continue
            
            # If we get here, that means we met all the requirements for
            # triggering this handler
            try:
                function(high_event)
            except:
                logger.exception('Exception in IRC handler')

    def _remove_connection(self, connection):
        """Removes a connection from the connection list."""
        self.connections.remove(connection)
    
    def _ping_ponger(self, connection, event):
        """Internal responder to PING events."""
        connection._last_ping = time.time()
        connection.pong(event.target)
    
    def _ctcp_handler(self, server, event):
        """Internal handler of CTCP events.
        
        Responds to VERSION, PING, TIME and SOURCE.
        
        The attributes :attr:`self.ctcp_version` and :attr:`ctcp_source` can
        be used to customize the responses of their respective CTCPs. 
        """
        ctcp = event.argument[0]
        parameters = event.argument[1:]
        
        source = event.source
        if '!' in source:
            # Source is a userhost, we need to split it 
            source = utils.nm_to_n(source)
        
        
        if ctcp == 'VERSION':
            server.ctcp_reply(source, 'VERSION ' + self.ctcp_version)
        elif ctcp == 'PING':
            # a ping ctcp has the caller's time in the argument
            ping_time = event.argument[1]
            server.ctcp_reply(source, 'PING ' + ping_time)
        elif ctcp == 'TIME':
            the_time = time.localtime()
            time_str = time.strftime("%a %b %d %Y %H:%M:%S", the_time)
            server.ctcp_reply(source, 'TIME ' + time_str)
        elif ctcp == 'SOURCE':
            server.ctcp_reply(source, 'SOURCE ' + self.ctcp_source)

Session.handlers = {}

class HighEvent(object):
    """
    A abstracted event of the IRC library.
    """
    def __init__(self, server, command, nickname, channel, message):
        super(HighEvent, self).__init__()
        
        self.command = command
        self.nickname = nickname
        self.server = server
        self.channel = channel
        self.message = message
        
    @classmethod
    def from_low_event(cls, server, low_event):
        command = low_event.eventtype
        
        # We supply the source and server already to reduce code repetition.
        # Just use it as the HighEvent constructor but with partial applied.
        creator = lambda *args, **kwargs: cls(server,
                                              command,
                                              *args,
                                              **kwargs)
        
        if command == 'welcome':
            # We treat this as a "connected" event
            # The name of the server we are connected to
            server_name = low_event.source
            # Our nickname - this might be different than the one we wanted!
            nickname = Nickname(low_event.target, nickname_only=True)
            # The welcome message
            message = low_event.argument[0]
            
            event = creator(nickname, None, message)
            event.command = 'connect'
            event.server_name = server_name
            return event
        elif command == 'nick':
            # A nickname change.
            old_nickname = Nickname(low_event.source)
            
            # We cheat here by using the original host and replacing the
            # name attribute with our new nickname.
            new_nickname = Nickname(low_event.source)
            new_nickname.name = low_event.target
            
            event = creator(old_nickname, None, None)
            event.new_nickname = new_nickname
            return event
        elif command in ["pubmsg", "pubnotice"]:
            # A channel message
            nickname = Nickname(low_event.source)
            channel = low_event.target
            message = low_event.argument[0]
            event = creator(nickname, channel, message)
            event.text_command = command
            event.command = 'text'
            return event
        elif command in ["privmsg", "privnotice"]:
            # Private message
            # The target is set to our own nickname in privmsg.
            nickname = Nickname(low_event.source)
            message = low_event.argument[0]
            event = creator(nickname, None, message)
            event.text_command = command
            event.command = 'text'
            return event
        elif command == 'ctcp':
            # A CTCP to us.
            # Same as privmsg/notice the target is our own nickname
            nickname = Nickname(low_event.source)
            # The irclib splits off the first space delimited word for us.
            # This is the CTCP command name
            ctcp = low_event.argument[0]
            # The things behind the command are then indexed behind it.
            message = ' '.join(low_event.argument[1:])
            
            event = creator(nickname, None, message)
            event.ctcp = ctcp
            return event
        elif command == 'action':
            # ACTION CTCP are parsed differently than others (for some reason)
            nickname = Nickname(low_event.source)
            # The target is present in an ACTION
            # However, this may be our nick; discard in that case
            channel = low_event.target
            if not server.is_channel(channel):
                channel = None
            # Message is in the argument
            message = low_event.argument[0]
            
            event = creator(nickname, channel, message)
            return event
        elif command == 'ctcpreply':
            # A CTCP reply.
            # Same as privmsg/notice the target is our own nickname
            nickname = Nickname(low_event.source)
            # The irclib splits off the first space delimited word for us.
            # This is the CTCP command name
            ctcp = low_event.argument[0]
            # The things behind the command are then indexed behind it.
            message = ' '.join(low_event.argument[1:])
            
            event = creator(nickname, None, message)
            event.ctcp = ctcp
            return event
        elif command == 'quit':
            # A quit from an user.
            nickname = Nickname(low_event.source)
            message = low_event.argument[0]
            
            return creator(nickname, None, message)
        elif command == 'join':
            # Someone joining our channel
            nickname = Nickname(low_event.source)
            channel = low_event.target
            
            return creator(nickname, channel, None)
        elif command == 'part':
            # Someone leaving our channel
            nickname = Nickname(low_event.source)
            channel = low_event.target
            
            return creator(nickname, channel, None)
        elif command == 'kick':
            # Someone forcibly leaving our channel.
            # The person kicking here
            kicker = Nickname(low_event.source)
            # The person being kicked
            target = Nickname(low_event.argument[0], nickname_only=True)
            # The reason given by the kicker
            reason = low_event.argument[1]
            # The channel this all went wrong in!
            channel = low_event.target
            
            event = creator(target, channel, reason)
            event.kicker = kicker
            return event
        elif command == 'invite':
            # Someone has invited us to a channel.
            # The inviter
            nickname = Nickname(low_event.source)
            # Target contains our nickname
            # First argument is the channel we were invited to
            channel = low_event.argument[0]
            return creator(nickname, channel, None)
        elif command in ['mode', 'umode']:
            # Mode change in the channel
            # The nickname that set the mode
            mode_setter = Nickname(low_event.source)
            # Simple channel
            channel = low_event.target
            
            # ServerConnection._parse_modes returns a list of tuples with
            # (operation, mode, param)
            # HOWEVER, we preparse the modes, so we (preferably) only want the
            # first one. Let's make sure we can still get all of them, though
            event = creator(mode_setter, channel, None)
            modes = server._parse_modes(' '.join(low_event.argument))
            if len(modes) > 1:
                event.modes = modes
            else:
                event.modes = modes[0]
            return event
        elif command in ['topic', 'currenttopic', 'notopic']:
            # Any message that tells us what the topic is.
            # The channel that had its topic set.
            if command == 'currenttopic':
                channel = low_event.argument[0]
            else:
                channel = low_event.target
            # The person who set the topic.
            # If this isn't a topic command, there is no setter
            topic_setter = None
            if command == 'topic':
                setter = Nickname(low_event.source)            
            # The argument contains the topic string
            # Treat notopic as empty string
            topic = ''
            if command == 'currenttopic':
                topic = ' '.join(low_event.argument[1:])
            elif command == 'topic':
                topic = low_event.argument[0]
            event = creator(topic_setter, channel, topic)
            event.command = 'topic'
            return event
        elif command == 'all_raw_messages':
            # This event contains all messages, unparsed
            server_name = low_event.source
            event = creator(None, None, low_event.argument[0])
            event.command = 'raw'
            return event
        
        # The event was not high level: thus, it's not raw, but simply unparsed
        # You will probably be able to register to these, but they won't have
        # much use
        event = creator(None, None, low_event.argument[0])
        event.source = low_event.source
        event.target = low_event.target
        return event
    
    
class Nickname(object):
    """
    A simple class that represents a nickname on IRC.
    
    Contains information such as actual nickname, hostmask and more.
    """
    def __init__(self, host, nickname_only=False):
        """
        The constructor really just expects the raw host send by IRC servers.
        
        it parses this for you into segments.
        
        if `nickname_only` is set to True it expects a bare nickname unicode
        object to be used as nickname and nothing more.
        """
        super(Nickname, self).__init__()
        
        if nickname_only:
            self.name = host
        else:
            self.name = utils.nm_to_n(host)
            self.host = host

def event_handler(events, channels=[], nicks=[], modes='', regex=''):
    """
    The decorator for high level event handlers. By decorating a function
    with this, the function is registered in the global :class:`Session` event
    handler list, :attr:`Session.handlers`.
    
        :param events: The events that the handler should subscribe to.
                        This can be both a string and a list; if a string
                        is provided, it will be added as a single element
                        in a list of events.
                        This rule applies to `channels` and `nicks` as well.
        
        :param channels: The channels that the events should trigger on.
                          Given an empty list, all channels will trigger
                          the event.
        
        :param nicks: The nicknames that this handler should trigger for.
                       Given an empty list, all nicknames will trigger
                       the event.
        
        :param modes: The required channel modes that are needed to trigger
                       this event.
                       If an empty mode string is specified, no modes are needed
                       to trigger the event.
        
        :param regex: The event will only be triggered if the
                       :attr:`HighEvent.message` matches the specified regex.
                       If no regex is specified, any :attr:`HighEvent.message`
                       will do.
    
    
    
    
    """
    Handler = collections.namedtuple('Handler', ['handler',
                                                 'events',
                                                 'channels',
                                                 'nicks',
                                                 'modes',
                                                 'regex'])
    
    # If you think the type checking here is wrong, please fix it,
    # i have no idea what i'm doing
    if not isinstance(events, list):
        events = [events]
    if not isinstance(channels, list):
        channels = [channels]
    if not isinstance(nicks, list):
        nicks = [nicks]
    if not isinstance(modes, str) and not isinstance(modes, unicode):
        raise TypeError('invalid type for mode string: {}'.format(modes))
    if not isinstance(regex, str) and not isinstance(regex, unicode):
        raise TypeError('invalid type for regex: {}'.format(regex))
    
    for event in events:
        if not isinstance(event, str) and not isinstance(event, unicode):
            raise TypeError('invalid type for event name: {}'.format(event))
    for channel in channels:
        if not isinstance(channel, str) and not isinstance(channel, unicode):
            raise TypeError('invalid type for channel name: {}'.format(channel))
    for nick in nicks:
        if not isinstance(nick, str) and not isinstance(nick, unicode):
            raise TypeError('invalid type for nickname: {}'.format(nick))
    
    # we don't care about cases, just lower
    events = map(lambda e: e.lower(), events)
    channels = map(lambda c: c.lower(), channels)
    nicks= map(lambda n: n.lower(), nicks)
    
    def decorator(fn):
        if regex != '':
            cregex = re.compile(regex, re.I)
        else:
            cregex = None
        handler = Handler(fn, events, channels, nicks, modes, cregex)
        Session.handlers[fn.__module__ + ":" + fn.__name__] = handler
        return fn
    return decorator
