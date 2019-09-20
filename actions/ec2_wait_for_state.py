#!/usr/bin/env python

from lib import action


class WaitManager(action.BaseAction):

    def run(self, instance_id, state, account_id=None, region=None):
        self.change_credentials(account_id, region)
        return self.wait_for_state(instance_id, state)
