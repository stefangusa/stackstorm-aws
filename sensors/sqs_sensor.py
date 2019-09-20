"""
This is generic SQS Sensor using boto3 api to fetch messages from sqs queue.
After receiving a message it's content is passed as payload to a trigger 'aws.sqs_new_message'
This sensor can be configured either by using config.yaml within a pack or by creating
following values in datastore:
    - aws.input_queues (list queues as comma separated string: first_queue,second_queue)
    - aws.aws_access_key_id
    - aws.aws_secret_access_key
    - aws.region
    - aws.max_number_of_messages (must be between 1 - 10)
For configuration in config.yaml with config like this
    setup:
      aws_access_key_id:
      aws_access_key_id:
      region:
    sqs_sensor:
      input_queues:
        - first_queue
        - second_queue
    sqs_other:
        max_number_of_messages: 1
If any value exist in datastore it will be taken instead of any value in config.yaml
"""

import boto3
import re
import six
import json
from boto3.session import Session
from botocore.exceptions import ClientError
from botocore.exceptions import NoRegionError
from botocore.exceptions import NoCredentialsError
from botocore.exceptions import EndpointConnectionError

from st2reactor.sensor.base import PollingSensor


class TestAWSSQSSensor(PollingSensor):
    def __init__(self, sensor_service, config=None, poll_interval=5):
        super(TestAWSSQSSensor, self).__init__(sensor_service=sensor_service, config=config,
                                           poll_interval=poll_interval)

    def setup(self):
        self._logger = self._sensor_service.get_logger(name=self.__class__.__name__)

        self.account_id = self._get_account_id()
        self.cross_roles_arns = self._get_cross_account_ids() or {}
        self.credentials = {}
        self.sessions = {}
        self.sqs_res = {}

    def poll(self):
        # setting SQS ServiceResource object from the parameter of datastore or configuration file
        self._may_setup_sqs()

        for queue in self.input_queues:
            if self._check_queue_if_url(queue):
                account_id = self._get_account_id_from_queue_url(queue)
                region = self._get_region_from_queue_url(queue)
            else:
                account_id = self.account_id
                region = self.aws_region

            msgs = self._receive_messages(queue=self._get_queue(queue, account_id, region),
                                          num_messages=self.max_number_of_messages)
            for msg in msgs:
                if msg:
                    payload = {"queue": queue,
                               "account_id": account_id,
                               "region": region,
                               "body": json.loads(msg.body)}
                    self._sensor_service.dispatch(trigger="aws.test_sqs_new_message", payload=payload)
                    msg.delete()

    def cleanup(self):
        pass

    def add_trigger(self, trigger):
        # This method is called when trigger is created
        pass

    def update_trigger(self, trigger):
        # This method is called when trigger is updated
        pass

    def remove_trigger(self, trigger):
        pass

    def _get_account_id(self):
        return boto3.client('sts').get_caller_identity().get('Account')

    def _get_cross_account_ids(self):
        cross_roles_arns = {}
        cross_roles_arns_list = self._get_config_entry('roles_arns') or []

        for arn in cross_roles_arns_list:
            cross_roles_arns[arn.split(':')[4]] = arn

        return cross_roles_arns

    def _get_config_entry(self, key, prefix=None):
        ''' Get configuration values either from Datastore or config file. '''
        config = self.config
        if prefix:
            config = self.config.get(prefix, {})

        value = self._sensor_service.get_value('aws.%s' % (key), local=False)
        if not value:
            value = config.get(key, None)

        if not value and config.get('setup', None):
            value = config['setup'].get(key, None)

        return value

    def _may_setup_sqs(self):
        queues = self._get_config_entry(key='input_queues', prefix='sqs_sensor')

        # XXX: This is a hack as from datastore we can only receive a string while
        # from config.yaml we can receive a list
        if isinstance(queues, six.string_types):
            self.input_queues = [x.strip() for x in queues.split(',')]
        elif isinstance(queues, list):
            self.input_queues = queues
        else:
            self.input_queues = []

        self.credentials[self.account_id] = (self._get_config_entry('aws_access_key_id'),
                                             self._get_config_entry('aws_secret_access_key'),
                                             None)
        self.aws_region = self._get_config_entry('region')
        self.max_number_of_messages = self._get_config_entry('max_number_of_messages',
                                                             prefix='sqs_other')

        # checker configuration is update, or not
        def _is_same_credentials(session, account_id):
            c = session.get_credentials()

            same_credentials = c is not None and \
                c.access_key == self.credentials[account_id][0] and \
                c.secret_key == self.credentials[account_id][1]

            if account_id != self.account_id:
                return same_credentials and c.token == self.credentials[account_id][2]
            else:
                return same_credentials

        for input_queue in self.input_queues:
            account_id = self._get_account_id_from_queue_url(input_queue)
            session = self.sessions.get(account_id, None)

            same_credentials = _is_same_credentials(session, account_id) if session else False
            if session is None or not same_credentials:
                session = self._setup_session() if account_id == self.account_id else \
                    self._setup_multiaccount_session(account_id)

            region = self._get_region_from_queue_url(input_queue)
            if not same_credentials:
                self._setup_sqs(session, account_id, region)

    def _setup_session(self):
        ''' Setup Boto3 structures '''
        self._logger.debug('Setting up SQS resources')
        session = Session(aws_access_key_id=self.credentials[self.account_id][0],
                          aws_secret_access_key=self.credentials[self.account_id][1])

        self.sessions[self.account_id] = session
        return session

    def _setup_multiaccount_session(self, account_id):
        try:
            assumed_role = boto3.client('sts').assume_role(
                RoleArn=self.cross_roles_arns[account_id],
                RoleSessionName='StackStormEvents'
            )
        except ClientError:
            self._logger.error('Could not assume role on %s', account_id)
            return
        self.credentials[account_id] = (assumed_role["Credentials"]["AccessKeyId"],
                                        assumed_role["Credentials"]["SecretAccessKey"],
                                        assumed_role["Credentials"]["SessionToken"])

        session = Session(
            aws_access_key_id=self.credentials[account_id][0],
            aws_secret_access_key=self.credentials[account_id][1],
            aws_session_token=self.credentials[account_id][2]
        )
        self.sessions[account_id] = session
        return session

    def _setup_sqs(self, session, account_id, region):
        try:
            if not self.sqs_res.get(account_id, None):
                self.sqs_res[account_id] = {}
            self.sqs_res[account_id][region] = session.resource('sqs', region_name=region)
        except NoRegionError:
            self._logger.warning("The specified region '%s' is invalid", region)

    def _check_queue_if_url(self, queue_url):
        reg = re.compile(r"https?://")
        if reg.match(queue_url[:7]) or reg.match(queue_url[:8]):
            return True
        return False

    def _get_account_id_from_queue_url(self, queue_url):
        if self._check_queue_if_url(queue_url):
            return queue_url.split('/')[3]
        return self.account_id

    def _get_region_from_queue_url(self, queue_url):
        if self._check_queue_if_url(queue_url):
            return queue_url.split('.')[1]
        return self.aws_region

    def _get_queue(self, queue, account_id, region):
        ''' Fetch QUEUE by it's name create new one if queue doesn't exist '''
        try:
            sqs_res = self.sqs_res[account_id][region]
            if account_id == self.account_id and region == self.aws_region:
                return sqs_res.get_queue_by_name(QueueName=queue)
            else:
                return sqs_res.Queue(queue)
        except ClientError as e:
            if e.response['Error']['Code'] == 'AWS.SimpleQueueService.NonExistentQueue' and \
                    account_id == self.account_id and region == self.aws_region:
                self._logger.warning("SQS Queue: %s doesn't exist, creating it.", queue)
                return sqs_res.create_queue(QueueName=queue)
            elif e.response['Error']['Code'] == 'InvalidClientTokenId':
                self._logger.warning("Couldn't operate sqs because of invalid credential config")
            else:
                raise
        except NoCredentialsError as e:
            self._logger.warning("Couldn't operate sqs because of invalid credential config")
        except EndpointConnectionError as e:
            self._logger.warning(e)

    def _receive_messages(self, queue, num_messages, wait_time=2):
        ''' Receive a message from queue and return it. '''
        if queue:
            return queue.receive_messages(WaitTimeSeconds=wait_time,
                                          MaxNumberOfMessages=num_messages)
        else:
            return []
