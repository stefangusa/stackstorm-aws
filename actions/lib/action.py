import re
import eventlet
import importlib

import boto.cloudformation
import boto.ec2
import boto.route53
import boto.vpc
import boto3

from st2common.runners.base_action import Action
from ec2parsers import ResultSets


class BaseAction(Action):

    def __init__(self, config):
        super(BaseAction, self).__init__(config)

        self.credentials = {
            'region': None,
            'aws_access_key_id': None,
            'aws_secret_access_key': None
        }
        self.userdata = None

        if config.get('st2_user_data', None):
            try:
                with open(config['st2_user_data'], 'r') as fp:
                    self.userdata = fp.read()
            except IOError as e:
                self.logger.error(e)

        # Note: In old static config credentials and region are under "setup" key and with a new
        # dynamic config values are top-level
        access_key_id = config.get('aws_access_key_id', None)
        secret_access_key = config.get('aws_secret_access_key', None)
        region = config.get('region', None)

        if access_key_id == "None":
            access_key_id = None
        if secret_access_key == "None":
            secret_access_key = None

        if access_key_id and secret_access_key:
            self.credentials['aws_access_key_id'] = access_key_id
            self.credentials['aws_secret_access_key'] = secret_access_key
        elif 'setup' in config:
            # Assume old-style config
            self.credentials = config['setup']

        if region:
            self.credentials['region'] = region

        self.resultsets = ResultSets()

    def ec2_connect(self):
        region = self.credentials['region']
        del self.credentials['region']
        return boto.ec2.connect_to_region(region, **self.credentials)

    def vpc_connect(self):
        region = self.credentials['region']
        del self.credentials['region']
        return boto.vpc.connect_to_region(region, **self.credentials)

    def cf_connect(self):
        region = self.credentials['region']
        del self.credentials['region']
        return boto.cloudformation.connect_to_region(region, **self.credentials)

    def r53_connect(self):
        route53_credentials = {
            key: self.credentials[key] for key in self.credentials if key != 'region'
        }
        return boto.route53.connection.Route53Connection(**route53_credentials)

    def get_r53zone(self, zone):
        conn = self.r53_connect()
        return conn.get_zone(zone)

    def st2_user_data(self):
        return self.userdata

    def get_boto3_session(self, resource):
        region = self.credentials['region']
        del self.credentials['region']
        return boto3.client(resource, region_name=region, **self.credentials)

    def split_tags(self, tags):
        tag_dict = {}
        split_tags = tags.split(',')
        for tag in split_tags:
            if re.search('=', tag):
                k, v = tag.split('=', 1)
                tag_dict[k] = v
        return tag_dict

    def wait_for_state(self, instance_id, state, timeout=10, retries=3):
        state_list = {}
        obj = self.ec2_connect()
        eventlet.sleep(timeout)
        instance_list = []

        for _ in range(retries + 1):
            try:
                instance_list = obj.get_only_instances([instance_id, ])
            except Exception:
                self.logger.info("Waiting for instance to become available")
                eventlet.sleep(timeout)

        for instance in instance_list:
            try:
                current_state = instance.update()
            except Exception, e:
                self.logger.info("Instance (%s) not listed. Error: %s" %
                                 (instance_id, e))
                eventlet.sleep(timeout)

            while current_state != state:
                current_state = instance.update()
            state_list[instance_id] = current_state
        return state_list

    def do_method(self, module_path, cls, action, **kwargs):
        module = importlib.import_module(module_path)
        # hack to connect to correct region
        if cls == 'EC2Connection':
            obj = self.ec2_connect()
        elif cls == 'VPCConnection':
            obj = self.vpc_connect()
        elif cls == 'CloudFormationConnection':
            obj = self.cf_connect()
        elif module_path == 'boto.route53.zone' and cls == 'Zone':
            zone = kwargs['zone']
            del kwargs['zone']
            obj = self.get_r53zone(zone)
        elif 'boto3' in module_path:
            for k, v in kwargs.items():
                if not v:
                    del kwargs[k]
            obj = self.get_boto3_session(cls)
        else:
            del self.credentials['region']
            obj = getattr(module, cls)(**self.credentials)

        if not obj:
            raise ValueError('Invalid or missing credentials (aws_access_key_id,'
                             'aws_secret_access_key) or region')

        method_fqdn = '%s.%s.%s' % (module_path, cls, action)
        self.logger.debug('Calling method "%s" with kwargs: %s' % (method_fqdn, str(kwargs)))

        resultset = getattr(obj, action)(**kwargs)
        formatted = self.resultsets.formatter(resultset)
        return formatted if isinstance(formatted, list) else [formatted]

    def do_function(self, module_path, action, **kwargs):
        module = __import__(module_path)

        function_fqdn = '%s.%s' % (module_path,  action)
        self.logger.debug('Calling function "%s" with kwargs: %s' % (function_fqdn, str(kwargs)))

        return getattr(module, action)(**kwargs)
