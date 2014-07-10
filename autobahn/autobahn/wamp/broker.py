###############################################################################
##
##  Copyright (C) 2013-2014 Tavendo GmbH
##
##  Licensed under the Apache License, Version 2.0 (the "License");
##  you may not use this file except in compliance with the License.
##  You may obtain a copy of the License at
##
##      http://www.apache.org/licenses/LICENSE-2.0
##
##  Unless required by applicable law or agreed to in writing, software
##  distributed under the License is distributed on an "AS IS" BASIS,
##  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
##  See the License for the specific language governing permissions and
##  limitations under the License.
##
###############################################################################

from __future__ import absolute_import
import six

from autobahn import util
from autobahn.wamp import types
from autobahn.wamp import role
from autobahn.wamp import message
from autobahn.wamp.exception import ApplicationError
from autobahn.wamp.interfaces import IBroker

from autobahn.wamp.message import _URI_PAT_STRICT_NON_EMPTY, _URI_PAT_LOOSE_NON_EMPTY



class SubscriptionOptions:
   """
   Subscription options used by the Broker to manage subscriptions
   """
   def __init__(self, metaonly = None, metatopics = None, match = None):
      self.metaonly = metaonly if metaonly is not None else False
      self.metatopics = metatopics if metatopics is not None else set()
      self.match = match if match is not None else message.Subscribe.MATCH_EXACT

   def __eq__(self, other):
      return (self.metaonly == other.metaonly) and \
             (self.metatopics == other.metatopics) and \
             (self.match == other.match)



class Subscription:
   """
   A subscription holding the subscription options and the subscribers
   """
   def __init__(self, topic, options):
      self.id = util.id()
      self.topic = topic
      self.options = options
      self.subscribers = set()



class Broker:
   """
   Basic WAMP broker, implements :class:`autobahn.wamp.interfaces.IBroker`.
   """

   def __init__(self, realm, options = None):
      """
      Constructor.

      :param realm: The realm this broker is working for.
      :type realm: str
      :param options: Router options.
      :type options: Instance of :class:`autobahn.wamp.types.RouterOptions`.
      """
      self.realm = realm
      self._options = options or types.RouterOptions()

      ## map: session -> set(subscription)
      ## needed for removeSession
      self._session_to_subscriptions = {}

      ## map: session_id -> session
      ## needed for exclude/eligible
      self._session_id_to_session = {}

      ## map: match t-> ( topic -> set(subscriptions) )
      ## needed for PUBLISH and SUBSCRIBE
      self._topic_to_subscriptions = {
         message.Subscribe.MATCH_EXACT: {},
         message.Subscribe.MATCH_PREFIX: {},
         message.Subscribe.MATCH_WILDCARD: {}
      }

      ## map: subscription_id -> subscription
      ## needed for UNSUBSCRIBE
      self._subscription_id_to_subscription = {}

      ## check all topic URIs with strict rules
      self._option_uri_strict = self._options.uri_check == types.RouterOptions.URI_CHECK_STRICT

      ## supported features from "WAMP Advanced Profile"
      self._role_features = role.RoleBrokerFeatures(publisher_identification = True,
                                                    subscriber_blackwhite_listing = True,
                                                    publisher_exclusion = True,
                                                    subscriber_metaevents = True,
                                                    pattern_based_subscription = True)


   def attach(self, session):
      """
      Implements :func:`autobahn.wamp.interfaces.IBroker.attach`
      """
      assert(session not in self._session_to_subscriptions)

      self._session_to_subscriptions[session] = set()
      self._session_id_to_session[session._session_id] = session


   def detach(self, session):
      """
      Implements :func:`autobahn.wamp.interfaces.IBroker.detach`
      """
      assert(session in self._session_to_subscriptions)

      ## Take copy because _unsubscribe modifies _session_to_subscriptions
      ##
      for subscription in set(self._session_to_subscriptions[session]):
         self._unsubscribe(session, subscription)

      del self._session_to_subscriptions[session]
      del self._session_id_to_session[session._session_id]


   def processPublish(self, session, publish):
      """
      Implements :func:`autobahn.wamp.interfaces.IBroker.processPublish`
      """
      assert(session in self._session_to_subscriptions)

      ## check topic URI
      ##
      if (not self._option_uri_strict and not  _URI_PAT_LOOSE_NON_EMPTY.match(publish.topic)) or \
         (    self._option_uri_strict and not _URI_PAT_STRICT_NON_EMPTY.match(publish.topic)):

         if publish.acknowledge:
            reply = message.Error(message.Publish.MESSAGE_TYPE, publish.request, ApplicationError.INVALID_URI, ["publish with invalid topic URI '{}'".format(publish.topic)])
            session._transport.send(reply)

         return

      publication = util.id()

      ## send publish acknowledge when requested
      ##
      if publish.acknowledge:
         msg = message.Published(publish.request, publication)
         session._transport.send(msg)

      ## remove publisher
      ##
      if publish.excludeMe is None or publish.excludeMe:
         me_also = False
      else:
         me_also = True

      for subscription in self._get_subscriptions(publish.topic):
         if not subscription.options.metaonly:
            ## Pattern based subscriptions need the exact topic
            ##
            if subscription.options.match != message.Subscribe.MATCH_EXACT:
               exact_topic = publish.topic
            else:
               exact_topic = None

            ## initial list of receivers are all subscribers ..
            ##
            receivers = subscription.subscribers

            ## filter by "eligible" receivers
            ##
            if publish.eligible:
               eligible = []
               for s in publish.eligible:
                  if s in self._session_id_to_session:
                     eligible.append(self._session_id_to_session[s])

               receivers = set(eligible) & receivers

            ## remove "excluded" receivers
            ##
            if publish.exclude:
               exclude = []
               for s in publish.exclude:
                  if s in self._session_id_to_session:
                     exclude.append(self._session_id_to_session[s])
               if exclude:
                  receivers = receivers - set(exclude)

            ## if receivers is non-empty, dispatch event ..
            ##
            if receivers:
               if publish.discloseMe:
                  publisher = session._session_id
               else:
                  publisher = None

               msg = message.Event(subscription.id,
                                   publication,
                                   args = publish.args,
                                   kwargs = publish.kwargs,
                                   publisher = publisher,
                                   topic = exact_topic)
               for receiver in receivers:
                  if me_also or receiver != session:
                     ## the subscribing session might have been lost in the meantime ..
                     if receiver._transport:
                        receiver._transport.send(msg)


   def processSubscribe(self, session, subscribe):
      """
      Implements :func:`autobahn.wamp.interfaces.IBroker.processSubscribe`
      """
      assert(session in self._session_to_subscriptions)

      ## check topic URI
      ##
      if (not self._option_uri_strict and not  _URI_PAT_LOOSE_NON_EMPTY.match(subscribe.topic)) or \
         (    self._option_uri_strict and not _URI_PAT_STRICT_NON_EMPTY.match(subscribe.topic)):

         reply = message.Error(message.Subscribe.MESSAGE_TYPE, subscribe.request, ApplicationError.INVALID_URI, ["subscribe for invalid topic URI '{}'".format(subscribe.topic)])
         session._transport.send(reply)
         return

      options = SubscriptionOptions(metaonly = subscribe.metaonly,
                                    metatopics = subscribe.metatopics,
                                    match = subscribe.match)

      ## Get the right slot for the subscription
      ##
      subscriptions = None
      for match, topic_to_subscriptions in six.iteritems(self._topic_to_subscriptions):
         if match == options.match:
            if not subscribe.topic in topic_to_subscriptions:
               topic_to_subscriptions[subscribe.topic] = set()
            subscriptions = topic_to_subscriptions[subscribe.topic]
            break

      assert(subscriptions is not None)

      ## Find subscription with the same options
      ##
      subscription = None
      for s in subscriptions:
         if s.options == options:
            subscription = s
            break

      if subscription is None:
         ## Create a new subscription
         ##
         subscription = Subscription(topic = subscribe.topic, options = options)
         self._subscription_id_to_subscription[subscription.id] = subscription
         subscriptions.add(subscription)

      if not session in subscription.subscribers:
         subscription.subscribers.add(session)

      if not subscription in self._session_to_subscriptions[session]:
         self._session_to_subscriptions[session].add(subscription)

      reply = message.Subscribed(subscribe.request, subscription.id)
      session._transport.send(reply)
      self._send_metaevent(session, subscription, message.Subscribe.METATOPIC_ADD)


   def processUnsubscribe(self, session, unsubscribe):
      """
      Implements :func:`autobahn.wamp.interfaces.IBroker.processUnsubscribe`
      """
      assert(session in self._session_to_subscriptions)

      if unsubscribe.subscription not in self._subscription_id_to_subscription:
         reply = message.Error(message.Unsubscribe.MESSAGE_TYPE, unsubscribe.request, ApplicationError.NO_SUCH_SUBSCRIPTION)
         session._transport.send(reply)
         return


      subscription = self._subscription_id_to_subscription[unsubscribe.subscription]
      reply = message.Unsubscribed(unsubscribe.request)
      session._transport.send(reply)
      self._unsubscribe(session, subscription)


   def _get_subscriptions(self, topic):
      subscriptions = set()

      if topic in self._topic_to_subscriptions[message.Subscribe.MATCH_EXACT]:
         subscriptions |= self._topic_to_subscriptions[message.Subscribe.MATCH_EXACT][topic]

      for prefix, s in six.iteritems(self._topic_to_subscriptions[message.Subscribe.MATCH_PREFIX]):
         if topic.startswith(prefix):
            subscriptions |= s

      ## FIXME
      #for pattern, s in six.iteritems(self._topic_to_subscriptions[message.Subscribe.MATCH_WILDCARD]):
      #   if pattern.matches(topic):
      #      subscriptions |= s

      return subscriptions


   def _unsubscribe(self, session, subscription):
      subscription.subscribers.discard(session)
      self._session_to_subscriptions[session].discard(subscription)

      if not subscription.subscribers:
         del self._subscription_id_to_subscription[subscription.id]

         for topic_to_subscriptions in six.itervalues(self._topic_to_subscriptions):
            if subscription.topic in topic_to_subscriptions:
               topic_to_subscriptions[subscription.topic].discard(subscription)

               if not topic_to_subscriptions[subscription.topic]:
                  del topic_to_subscriptions[subscription.topic]

               break

      self._send_metaevent(session, subscription, message.Subscribe.METATOPIC_REMOVE)


   def _send_metaevent(self, session, origin_subscription, metatopic):
      publication = util.id()
      for subscription in self._get_subscriptions(origin_subscription.topic):
         if metatopic in subscription.options.metatopics:
            ## Pattern based subscriptions need the exact topic.
            ## There is no PUBLISH.Topic for meta events so we send the origin_subscription.topic
            ##
            if (subscription.options.match != message.Subscribe.MATCH_EXACT) or (origin_subscription.options.match != message.Subscribe.MATCH_EXACT):
               exact_topic = origin_subscription.topic
            else:
               exact_topic = None
            event = message.Event(subscription.id,
                                  publication,
                                  metatopic = metatopic,
                                  session = session._session_id,
                                  topic = exact_topic)
            for subscriber in subscription.subscribers:
               if not (subscription == origin_subscription and subscriber == session) and subscriber._transport:
                  subscriber._transport.send(event)



IBroker.register(Broker)
