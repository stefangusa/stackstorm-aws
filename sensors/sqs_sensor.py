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

import six
import json
from boto3.session import Session
from botocore.exceptions import ClientError
from botocore.exceptions import NoRegionError
from botocore.exceptions import NoCredentialsError
from botocore.exceptions import EndpointConnectionError

from st2reactor.sensor.base import PollingSensor


class AWSSQSSensor(PollingSensor):
    def __init__(self, sensor_service, config=None, poll_interval=5):
        super(AWSSQSSensor, self).__init__(sensor_service=sensor_service, config=config,
                                           poll_interval=poll_interval)

    def setup(self):
        self._logger = self._sensor_service.get_logger(name=self.__class__.__name__)

        self.account_id = None
        self.credentials = {}
        self.sessions = {}
        self.sqs_res = {}

        self.cross_roles_arns = {arn.split(':')[4]: arn for arn in self.config.get('roles_arns', [])}

    def poll(self):
        # setting SQS ServiceResource object from the parameter of datastore or configuration file
        self._may_setup_sqs()

        for queue in self.input_queues:
            account_id, region = self._get_info(queue)
            msgs = self._receive_messages(queue=self._get_queue(queue, account_id, region),
                                          num_messages=self.max_number_of_messages)
            for msg in msgs:
                if msg:
                    payload = {"queue": queue,
                               "account_id": account_id,
                               "region": region,
                               "body": json.loads(msg.body)}
                    self._sensor_service.dispatch(trigger="aws.sqs_new_message", payload=payload)
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

        self.aws_region = self._get_config_entry('region')
        self.max_number_of_messages = self._get_config_entry('max_number_of_messages', prefix='sqs_other')

        self.access_key_id = self._get_config_entry('aws_access_key_id')
        self.secret_access_key = self._get_config_entry('aws_secret_access_key')

        self._setup_session()

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
            account_id, region = self._get_info(input_queue)
            session = self.sessions.get(account_id, None)

            same_credentials = _is_same_credentials(session, account_id) if session else False

            if session is None or not same_credentials:
                if account_id == self.account_id:
                    self._setup_session()
                else:
                    self._setup_multiaccount_session(account_id, region)

    def _setup_session(self):
        ''' Setup Boto3 structures '''
        session = Session(aws_access_key_id=self.access_key_id,
                          aws_secret_access_key=self.secret_access_key)

        if not self.account_id:
            self.account_id = session.client('sts').get_caller_identity().get('Account')
            self.credentials[self.account_id] = (self.access_key_id, self.secret_access_key, None)

        self.sessions[self.account_id] = session
        self._setup_sqs(session, self.account_id, self.aws_region)

    def _setup_multiaccount_session(self, account_id, region):
        try:
            assumed_role = self.sessions[self.account_id].client('sts').assume_role(
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
        self._setup_sqs(session, account_id, region)

    def _setup_sqs(self, session, account_id, region):
        try:
            if not self.sqs_res.get(account_id, None):
                self.sqs_res[account_id] = {}
            self.sqs_res[account_id][region] = session.resource('sqs', region_name=region)
        except NoRegionError:
            self._logger.warning("The specified region '%s' is invalid", region)

    def _get_info(self, queue):
        if queue.startswith('http://') or queue.startswith('https://'):
            return queue.split('/')[3], queue.split('.')[1]
        return self.account_id, self.aws_region

    def _get_queue(self, queue, account_id, region):
        ''' Fetch QUEUE by it's name create new one if queue doesn't exist '''
        sqs_res = self.sqs_res[account_id][region]
        try:
            if queue.startswith('http://') or queue.startswith('https://'):
                return sqs_res.Queue(queue)
            else:
                return sqs_res.get_queue_by_name(QueueName=queue)
        except ClientError as e:
            if e.response['Error']['Code'] == 'AWS.SimpleQueueService.NonExistentQueue':
                self._logger.warning("SQS Queue: %s doesn't exist, creating it.", queue)
                if queue.startswith('http://') or queue.startswith('https://'):
                    return sqs_res.create_queue(QueueName=queue.split('/')[4])
                else:
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
