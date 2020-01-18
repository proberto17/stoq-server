# -*- coding: utf-8 -*-
# vi:si:et:sw=4:sts=4:ts=4

#
# Copyright (C) 2019 Stoq Tecnologia <https://www.stoq.com.br>
# All rights reserved
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., or visit: http://www.gnu.org/.
#
# Author(s): Stoq Team <stoq-devel@async.com.br>
#

import logging
import json
from typing import Dict

from flask import request, Response
from gevent.queue import Queue
from gevent.event import Event

from stoqlib.api import api
from stoqserver.lib.baseresource import BaseResource
from ..signals import TefCheckPendingEvent
from ..utils import JsonEncoder

log = logging.getLogger(__name__)


# pyflakes
Dict


class EventStreamBrokenException(Exception):
    pass


class EventStream(BaseResource):
    """A stream of events from this server to the application.

    Callsites can use EventStream.put(event) to send a message from the server to the client
    asynchronously.

    Note that there should be only one client connected at a time. If more than one are connected,
    all of them will receive all events
    """

    # Some queues that messages will be added to and latter be sent to the connected stations in the
    # stream. Note that there can be only one client for each station
    _streams = {}  # type: Dict[str, Queue]

    # The replies that is comming from the station.
    _replies = {}  # type: Dict[str, Queue]

    # Indicates if there is a payment process waiting for a reply from a station.
    _waiting_reply = {}  # type: Dict[str, Event]

    routes = ['/stream']

    @classmethod
    def put(cls, station, data):
        """Put a event only on the client stream"""
        assert station.id in cls._streams
        cls._streams[station.id].put(data)

    @classmethod
    def put_all(cls, data):
        """Put a event in all streams"""
        for stream in cls._streams.values():
            stream.put(data)

    @classmethod
    def ask_question(cls, station, question):
        """Sends a question down the stream"""
        log.info('Asking %s question: %s', station.name, question)
        cls.put(station, {
            'type': 'TEF_ASK_QUESTION',
            'data': question,
        })

        log.info('Waiting tef reply')
        cls._waiting_reply[station.id].set()
        reply = cls._replies[station.id].get()
        cls._waiting_reply[station.id].clear()
        log.info('Got tef reply: %s', reply)
        return reply

    @classmethod
    def put_reply(cls, station_id, reply):
        """Puts a reply from the frontend"""
        log.info('Got reply from %s: %s', station_id, reply)
        assert cls._replies[station_id].empty()
        assert cls._waiting_reply[station_id].is_set()

        return cls._replies[station_id].put(reply)

    def _loop(self, stream, station_id):
        while True:
            data = stream.get()
            yield "data: " + json.dumps(data, cls=JsonEncoder) + "\n\n"
        log.info('Closed event stream for %s', station_id)

    def get(self):
        stream = Queue()
        station = self.get_current_station(api.get_default_store(), token=request.args['token'])
        log.info('Estabilished event stream for %s', station.id)
        self._streams[station.id] = stream

        # Don't replace the reply queue and waiting reply flag
        self._replies.setdefault(station.id, Queue(maxsize=1))
        self._waiting_reply.setdefault(station.id, Event())

        if self._waiting_reply[station.id].is_set():
            # There is a new stream for this station, but we were currently waiting for a reply from
            # the same station in the previous event stream. Put an invalid reply there, and clear
            # the flag so that the station can continue working
            self._replies[station.id].put(EventStreamBrokenException)
            self._waiting_reply[station.id].clear()

        # If we dont put one event, the event stream does not seem to get stabilished in the browser
        stream.put(json.dumps({}))

        # This is the best time to check if there are pending transactions, since the frontend just
        # stabilished a connection with the backend (thats us).
        has_canceled = TefCheckPendingEvent.send()
        if has_canceled and has_canceled[0][1]:
            EventStream.put(station, {'type': 'TEF_WARNING_MESSAGE',
                                      'message': ('Última transação TEF não foi efetuada.'
                                                  ' Favor reter o Cupom.')})
            EventStream.put(station, {'type': 'CLEAR_SALE'})
        return Response(self._loop(stream, station.id), mimetype="text/event-stream")