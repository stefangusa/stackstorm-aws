from datetime import datetime, timedelta
import re
import requests

from botocore.exceptions import ClientError
from botocore.exceptions import NoCredentialsError
from botocore.exceptions import NoRegionError
from botocore.exceptions import EndpointConnectionError

from sqs_sensor import AWSSQSSensor


class AWSHealthEventsSQSSensor(AWSSQSSensor):
    def __init__(self, sensor_service, config=None, poll_interval=5):
        super(AWSHealthEventsSQSSensor, self).__init__(sensor_service=sensor_service, config=config,
                                                       poll_interval=poll_interval)

        self.alert_configs = self._get_alert_configs()

    def _create_body(self, session, body):
        if body['eventTypeCode'] != 'AWS_EC2_PERSISTENT_INSTANCE_RETIREMENT_SCHEDULED' and \
           body['eventTypeCode'] != 'AWS_EC2_INSTANCE_REBOOT_FLEXIBLE_MAINTENANCE_SCHEDULED':
            return None

        trigger_body = []
        try:
            ec2_session = session.resource('ec2', region_name=body['region'])
            cloudwatch_client = session.client('cloudwatch', region_name=body['region'])
        except NoRegionError:
            self._logger.error("Could not establish EC2 or CloudWatch session in region '%s' on account %s.",
                               body['region'], body['account'])
            return None

        for instance_id in body['resources']:
            instance_info = self._get_instance_details(ec2_session, cloudwatch_client, instance_id)
            if instance_info:
                trigger_body.append(instance_info)

        return trigger_body

    def _get_alert_configs(self):
        try:
            response = requests.get(url="http://va6-prod-alertmanager-1:8080/api/v1/status")
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            self._logger.warning("Request to alertmanager server failed with error:\n%s", e)
            self._logger.info("Trying with the other endpoint - alertmanager-2.")

            try:
                response = requests.get(url="http://va6-prod-alertmanager-2:8080/api/v1/status")
                response.raise_for_status()
            except requests.exceptions.RequestException as e:
                self._logger.error("Request to fallback alertmanager server failed with error:\n%s", e)
                raise

        configs = response.json()
        return {
            'routes': configs['data']['configJSON']['route']['routes'],
            'receivers': configs['data']['configJSON']['receivers']
        }

    def _get_instance_details(self, ec2_session, cloudwatch_client, instance_id):
        try:
            instance = ec2_session.Instance(instance_id)
        except (ClientError, EndpointConnectionError) as e:
            self._logger.error(e)
            return None
        except NoCredentialsError:
            self._logger.error('Invalid credentials for connecting to EC2 instance %s.', instance_id)
            return None

        tags = {tag['Key']: tag['Value'] for tag in instance.tags}

        if 'Adobe:ArchPath' not in tags:
            self._logger.error('EC2 instance %s does not have an ArchPath.', instance_id)
            return None

        slack_channels = self._get_slack_channels(tags.get('Adobe:ArchPath'))
        if not slack_channels:
            self._logger.error('No Slack channels are attached to %s configuration.', instance_id)

        instance_status_check_failed, system_status_check_failed = \
            self._get_instance_status_checks(cloudwatch_client, instance_id)

        return {
            'FQDN': tags.get('Adobe:FQDN', tags.get('hostname', tags.get('Name', tags.get('Adobe:ArchPath')))),
            'state': instance.state['Name'],
            'slack_channels': slack_channels,
            'instance_status_check_failed': instance_status_check_failed,
            'system_status_check_failed': system_status_check_failed
        }

    def _get_slack_channels(self, arch_path):
        matching_receiver = None

        for route in self.alert_configs['routes']:
            pattern = re.compile(route['match_re']['ArchPath'])
            if pattern.match(arch_path):
                matching_receiver = route['receiver']
                break

        if not matching_receiver:
            return []

        for receiver in self.alert_configs['receivers']:
            if receiver['name'] == matching_receiver and receiver.get('slack_configs') is not None:
                return [slack_config['channel'] for slack_config in receiver['slack_configs']]
        return []

    def _get_instance_status_checks(self, cloudwatch_client, instance_id):
        try:
            response = cloudwatch_client.get_metric_statistics(Namespace='AWS/EC2',
                MetricName='StatusCheckFailed_Instance',
                Dimensions=[{'Name': 'InstanceId', 'Value': instance_id}],
                StartTime=datetime.now() - timedelta(days=1),
                EndTime=datetime.now(),
                Period=86400,
                Statistics=['Maximum'])

            if response['Datapoints'][0]['Maximum'] == 0:
                instance_status_check_failed = 'No'
            else:
                instance_status_check_failed = 'Yes'
        except ClientError:
            self._logger.warning('Could not retrieve Instance Status Check of instance %s.', instance_id)
            instance_status_check_failed = 'Unavailable'

        try:
            response = cloudwatch_client.get_metric_statistics(Namespace='AWS/EC2',
                MetricName='StatusCheckFailed_Instance',
                Dimensions=[{'Name': 'InstanceId', 'Value': instance_id}],
                StartTime=datetime.now() - timedelta(days=1),
                EndTime=datetime.now(),
                Period=86400,
                Statistics=['Maximum'])

            if response['Datapoints'][0]['Maximum'] == 0:
                system_status_check_failed = 'No'
            else:
                system_status_check_failed = 'Yes'
        except ClientError:
            self._logger.warning('Could not retrieve System Status Check of instance %s.', instance_id)
            system_status_check_failed = 'Unavailable'

        return instance_status_check_failed, system_status_check_failed
