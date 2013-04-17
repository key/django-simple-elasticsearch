import collections

from django.db.models import signals
from django.core.signals import request_finished
from pyelasticsearch import ElasticSearch

from .settings import ES_USE_REQUEST_FINISHED_SIGNAL, ES_BULK_LIMIT_BEFORE_SEND


if ES_USE_REQUEST_FINISHED_SIGNAL:
    ES_REQUEST_FINISHED_DATA = collections.defaultdict(lambda: [])

    def process_bulk_data(sender, **kwargs):
        global ES_REQUEST_FINISHED_DATA

        # TODO: come up with way to batch ALL different index bulk data into 
        # a single set of data to maximize bulk calls
        
        # ask each ES index instance to handle its own 'sending' to ES
        for index, data in ES_REQUEST_FINISHED_DATA.iteritems():
            index.bulk_send(data)

        # Reset the global data object
        ES_REQUEST_FINISHED_DATA = collections.defaultdict(lambda: [])

    request_finished.connect(process_bulk_data, dispatch_uid=u'ES_USE_REQUEST_FINISHED_SIGNAL')


class ESDirectMixin:
    """ Use this mixin to enable auto-indexing/deleting through Django model
    save/delete signal handlers.
    """
    use_signals = True


class ESRabbitMQMixin:
    """ Use this mixin to enable auto-indexing/deleting through Django model
    save/delete signal handlers. Instead of communicating with ES directly, this
    mixin sends all single/bulk requests through RabbitMQ using the ES 'river'
    configuration.
    """
    use_signals = True

    def index_object(self, obj):
        pass

    def delete_object(self, obj):
        pass

    def bulk_send(self, data):
        pass


class ESBaseIndex(object):
    use_signals = False
    _es = None

    def __init__(self, *args, **kwargs):
        self._model = self.get_model()
        self._index_name = self.get_index_name()
        self._type_name = self.get_type_name()

        # Did we get an ElasticSearch object passed in? If so, use it
        self._es = kwargs.get('es', None)

    @property
    def es(self):
        if not self._es:
            self._es = ElasticSearch('http://localhost:9200/')
        return self._es

    def register_signals(self):
        # By default, use_signals is False to disable save/delete handling - leaving the user to manually
        # update the ES index via the management command included with this app or use one of
        # the provided mixins to enable signal handling.
        if self.use_signals:
            signals.post_save.connect(
                receiver=self.handle_save,
                sender=self._model,
                dispatch_uid=u"{0}.{1}.save".format(
                    self._model._meta.app_label,
                    self._model._meta.module_name
                )
            )
            signals.post_delete.connect(
                receiver=self.handle_delete,
                sender=self._model,
                dispatch_uid=u"{0}.{1}.delete".format(
                    self._model._meta.app_label,
                    self._model._meta.module_name
                )
            )

    def handle_save(self, sender, instance, **kwargs):
        self.perform_action(instance, 'index')

    def handle_delete(self, sender, instance, **kwargs):
        self.perform_action(instance, 'delete')

    def perform_action(self, obj, operation, bulk=ES_USE_REQUEST_FINISHED_SIGNAL):
        if obj and obj.pk:
            if operation == 'index' and self.should_index(obj):
                if bulk:
                    self.bulk_queue(obj, operation)
                else:
                    self.index_object(obj)
            elif operation == 'delete':
                if bulk:
                    self.bulk_queue(obj, operation)
                else:
                    self.delete_object(obj)

    def bulk_queue(self, obj, operation):
        global ES_REQUEST_FINISHED_DATA
        ES_REQUEST_FINISHED_DATA[self].append(self.bulk_prepare(obj, operation))

        # ensure we don't build up too big of a queue of data - we don't want
        # to eat up too much memory, so just send if we hit a threshold
        if len(ES_REQUEST_FINISHED_DATA[self]) >= ES_BULK_LIMIT_BEFORE_SEND:
            self.bulk_send(ES_REQUEST_FINISHED_DATA[self])

    def bulk_prepare(self, obj, operation):
        """ This method is building custom ES bulk-formatted lines so that we can send a
        custom request through pyelasticsearch as its bulk_index() implementation has parameter
        limitations. This simply JSON-dumps the appropriate structures to a string.
        """
        data = {
            '_index': self._index_name,
            '_type': self._type_name,
            "_id": obj.pk
        }
        data.update(self.get_object_params(obj))

        # bulk operation instruction line
        op = self.es._encode_json({operation: data}) + '\n'

        # bulk operation data line
        if operation.lower() != 'delete':
            op += self.es._encode_json(self.get_object_data(obj)) + '\n'

        return op

    def bulk_send(self, data):
        # We use a custom bulk operation request due to limitations of the current
        # design of pyelasticsearch's implementation; for more details, see the
        # queue_data() method comments
        self.es.send_request(
            'POST',
            ['_bulk'],
            ''.join(data),
            encode_body=False,
            query_params=self.get_bulk_operation_params()
        )

    def index_object(self, obj):
        self.es.index(
            self._index_name,
            self._type_name,
            self.get_object_data(obj),
            id=obj.pk,
            **self.get_object_params(obj)
        )

    def delete_object(self, obj):
        self.es.delete(
            self._index_name,
            self._type_name,
            id=obj.pk,
            **self.get_object_params(obj)
        )

    def get_index_name(self):
        return self._model._meta.app_label.lower()

    def get_type_name(self):
        return self._model._meta.module_name.lower()

    def get_queryset(self):
        return self._model._default_manager.all()

    def should_index(self, obj):
        return True

    def get_mapping(self):
        # default to letting ES 'auto' map/schema the data fields
        return {}

    def get_bulk_operation_params(self):
        return {}

    def get_object_params(self, obj):
        # get routing, ttl, timestamp, etc. options for the item
        return {}

    def get_object_data(self, obj):
        # This must return a dictionary
        raise NotImplementedError

    def get_model(self):
        raise NotImplementedError




# if amqp:
#     # TODO: rewrite the self.es.* calls below to send data to the rabbitmq river instead
#     class ESMessageQueueDriverMixin(ESBaseDriverMixin):
#         """ This mixin uses the concept of ES's Redis or RabbitMQ 'rivers' to send
#         bulk-index/delete formatted data to ES via Redis or RabbitMQ.
#         """

### instead of the below, perhaps use Kombu as we'll be able to create a redis driver too

#
#                 # conn = amqp.Connection(options.host, userid=options.userid,
#                 #                        password=options.password, ssl=options.ssl)
#                 #
#                 # ch = conn.channel()
#                 # ch.exchange_declare('myfan', 'fanout')
#                 #
#                 # msg = amqp.Message(msg_body, content_type='text/plain',
#                 #                    application_headers={'foo': 7, 'bar': 'baz'})
#                 #
#                 # ch.basic_publish(msg, 'myfan')
#                 #
#                 # ch.close()
#                 # conn.close()
#

