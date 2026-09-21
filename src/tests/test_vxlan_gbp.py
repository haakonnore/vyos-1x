# Copyright VyOS maintainers and contributors <maintainers@vyos.io>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 or later as
# published by the Free Software Foundation.

from copy import deepcopy
from unittest import TestCase
from unittest.mock import Mock, patch

from vyos import ConfigError
from vyos.configdict import get_vxlan_gbp_config
from vyos.configverify import verify_vrf, verify_vxlan_gbp


class TestVXLANGBP(TestCase):
    def setUp(self):
        self.interfaces = {
            'ethernet': {'eth1': {'vrf': 'red'}, 'eth2': {'vrf': 'blue'}},
            'vxlan': {
                'vxlan10': {
                    'gbp': {},
                    'source_interface': 'eth1',
                    'remote': ['192.0.2.1'],
                },
                'vxlan20': {'source_interface': 'eth2', 'remote': ['192.0.2.2']},
            },
        }

    def config(self):
        def get_config_dict(path, **kwargs):
            interfaces = deepcopy(self.interfaces)
            if path == ['interfaces']:
                return interfaces
            return interfaces.get('vxlan', {})

        config = Mock()
        config.get_level.return_value = ['interfaces', 'vxlan']
        config.get_config_dict.side_effect = get_config_dict
        return config

    def tunnels(self):
        config = self.config()
        result = get_vxlan_gbp_config(config)
        self.assertEqual(config.set_level.call_args_list[0].args, ([],))
        self.assertEqual(
            config.set_level.call_args_list[-1].args, (['interfaces', 'vxlan'],)
        )
        return result

    def test_separate_vrfs(self):
        verify_vxlan_gbp(self.tunnels())

    def test_uniform_gbp_skips_interfaces_tree(self):
        # Without mixed GBP settings nothing can conflict. Neither the
        # interfaces tree nor the running system is consulted.
        for gbp in [False, True]:
            with self.subTest(gbp=gbp):
                for tunnel in self.interfaces['vxlan'].values():
                    tunnel.pop('gbp', None)
                    if gbp:
                        tunnel['gbp'] = {}
                    tunnel['source_interface'] = 'pod-test'
                config = self.config()
                with patch('vyos.ifconfig.Interface') as interface:
                    tunnels = get_vxlan_gbp_config(config)
                    interface.assert_not_called()
                paths = [call.args[0] for call in config.get_config_dict.call_args_list]
                self.assertEqual(paths, [['interfaces', 'vxlan']])
                self.assertEqual(set(tunnels), {'vxlan10', 'vxlan20'})
                verify_vxlan_gbp(tunnels)
                verify_vrf({'ifname': 'eth2', 'vxlan_gbp_tunnels': tunnels})

    def test_vpp_interfaces_are_ignored(self):
        # "interfaces vpp" holds one tag node per type, not interfaces.
        self.interfaces['vpp'] = {
            'bonding': {'vppbond0': {'mode': 'lacp'}},
            'vxlan': {'vppvxlan1': {'vni': '10'}},
        }
        tunnels = self.tunnels()
        self.assertEqual(tunnels['vxlan10']['underlay_vrf'], 'red')
        self.assertEqual(tunnels['vxlan20']['underlay_vrf'], 'blue')
        verify_vxlan_gbp(tunnels)

    def test_pending_vrf_change_uses_candidate(self):
        self.interfaces['ethernet']['eth2']['vrf'] = 'red'
        with patch('vyos.ifconfig.Interface') as interface:
            interface.return_value.get_vrf.return_value = 'blue'
            with self.assertRaisesRegex(ConfigError, 'different "gbp" setting'):
                verify_vxlan_gbp(self.tunnels())
            interface.assert_not_called()

    def test_pending_vrf_removal_is_checked_without_vrf_key(self):
        del self.interfaces['ethernet']['eth2']['vrf']
        config = {'ifname': 'eth2', 'vxlan_gbp_tunnels': self.tunnels()}
        with self.assertRaisesRegex(ConfigError, 'different "gbp" setting'):
            verify_vrf(config)

    def test_vlan_and_qinq_bindings(self):
        configurations = [
            ('eth2.100', {'vif': {'100': {'vrf': 'red'}}}),
            ('eth2.100', {'vif_s': {'100': {'vrf': 'red'}}}),
            ('eth2.100.200', {'vif_s': {'100': {'vif_c': {'200': {'vrf': 'red'}}}}}),
        ]
        for source, config in configurations:
            with self.subTest(source=source, config=config):
                self.interfaces['ethernet']['eth2'] = config
                self.interfaces['vxlan']['vxlan20']['source_interface'] = source
                with self.assertRaisesRegex(ConfigError, 'different "gbp" setting'):
                    verify_vxlan_gbp(self.tunnels())

    def test_other_source_interface_types(self):
        for kind, source in [
            ('dummy', 'dum1'),
            ('bonding', 'bond0'),
            ('bridge', 'br0'),
        ]:
            with self.subTest(kind=kind):
                self.interfaces[kind] = {source: {'vrf': 'red'}}
                self.interfaces['vxlan']['vxlan20']['source_interface'] = source
                with self.assertRaisesRegex(ConfigError, 'different "gbp" setting'):
                    verify_vxlan_gbp(self.tunnels())

    def test_separate_ports_allow_shared_vrf(self):
        self.interfaces['ethernet']['eth2']['vrf'] = 'red'
        self.interfaces['vxlan']['vxlan20']['port'] = '55000'
        verify_vxlan_gbp(self.tunnels())

    def test_separate_families_allow_shared_vrf(self):
        self.interfaces['ethernet']['eth2']['vrf'] = 'red'
        self.interfaces['vxlan']['vxlan20']['remote'] = ['2001:db8::1']
        verify_vxlan_gbp(self.tunnels())

    def test_external_uses_both_families(self):
        self.interfaces['ethernet']['eth2']['vrf'] = 'red'
        self.interfaces['vxlan']['vxlan20']['remote'] = ['2001:db8::1']
        self.interfaces['vxlan']['vxlan10']['parameters'] = {'external': {}}
        with self.assertRaisesRegex(ConfigError, 'different "gbp" setting'):
            verify_vxlan_gbp(self.tunnels())

    def test_matching_gbp_allows_shared_vrf(self):
        self.interfaces['ethernet']['eth2']['vrf'] = 'red'
        self.interfaces['vxlan']['vxlan20']['gbp'] = {}
        verify_vxlan_gbp(self.tunnels())

    def test_overlay_vrfs_do_not_isolate_sockets(self):
        self.interfaces['ethernet']['eth2']['vrf'] = 'red'
        self.interfaces['vxlan']['vxlan10']['vrf'] = 'overlay1'
        self.interfaces['vxlan']['vxlan20']['vrf'] = 'overlay2'
        with self.assertRaisesRegex(ConfigError, 'different "gbp" setting'):
            verify_vxlan_gbp(self.tunnels(), 'vxlan10')
