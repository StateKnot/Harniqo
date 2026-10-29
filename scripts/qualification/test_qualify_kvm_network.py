#!/usr/bin/env python3
"""Offline negative guard tests; no VM/network/port creation or model calls."""
import ipaddress
import unittest
import xml.etree.ElementTree as ET

import qualify_kvm_network as network


class NetworkSafetyTests(unittest.TestCase):
    def test_subnet_selection_rejects_host_routes_and_larger_overlap(self):
        chosen = network.select_subnet([ipaddress.ip_network("172.29.240.0/24"), ipaddress.ip_network("172.29.241.2/32")])
        self.assertEqual(str(chosen), "172.29.241.16/28")
        with self.assertRaisesRegex(RuntimeError, "No non-overlapping"):
            network.select_subnet([ipaddress.ip_network("172.16.0.0/12")])

    def test_identity_mismatch_refuses_cleanup(self):
        actual = '<network><name>other</name><uuid>111</uuid><bridge name="existing"/></network>'
        with self.assertRaisesRegex(RuntimeError, "identity changed"):
            network.require_identity(actual, {"name":"mine", "uuid":"111", "bridge":"mine"})
        with self.assertRaisesRegex(RuntimeError, "bridge identity"):
            network.require_identity(actual, {"name":"other", "uuid":"111", "bridge":"mine"})
        with self.assertRaisesRegex(RuntimeError, "identity changed"):
            network.require_identity('<filter name="other"><uuid>111</uuid></filter>', {"name":"mine", "uuid":"111"}, filter_object=True)

    def test_network_has_no_dns_dhcp_forward_or_autostart(self):
        tree = ET.fromstring(network.network_xml({"name":"fixture", "uuid":"111", "bridge":"fixture"}, ipaddress.ip_network("192.0.2.0/28")))
        self.assertEqual(tree.find("dns").get("enable"),"no")
        self.assertEqual(tree.find("port").get("isolated"),"yes")
        self.assertIsNone(tree.find("forward"));self.assertEqual(tree.findall(".//dhcp"),[])

    def test_filter_never_allows_other_destination_or_port(self):
        item={"filter_name":"fixture","filter_uuid":"111","mac":"52:54:00:12:34:56","ip":"192.0.2.2"}
        tree=ET.fromstring(network.filter_xml(item,"192.0.2.1"))
        accepts=[x for x in tree.findall("rule") if x.get("action")=="accept" and x.find("tcp") is not None]
        self.assertEqual(len(accepts),2)
        outgoing=next(x.find("tcp") for x in accepts if x.get("direction")=="out")
        self.assertEqual(outgoing.get("dstipaddr"),"192.0.2.1")
        self.assertEqual(outgoing.get("dstportstart"),str(network.PORT))
        self.assertEqual(outgoing.get("dstportend"),str(network.PORT))
        self.assertEqual(outgoing.get("srcipaddr"),item["ip"])
        incoming=next(x.find("tcp") for x in accepts if x.get("direction")=="in")
        self.assertEqual(incoming.get("state"),"ESTABLISHED")
        self.assertEqual(incoming.get("srcipaddr"),"192.0.2.1")
        self.assertEqual(incoming.get("dstipaddr"),item["ip"])
        self.assertTrue(any(x.get("action")=="drop" and x.find("all") is not None for x in tree.findall("rule")))
        self.assertTrue(any(x.get("action")=="drop" and len(x)==0 for x in tree.findall("rule")))
        self.assertEqual([x for x in tree.findall(".//mac") if x.get("protocolid")=="ipv6"],[])

    def test_embedded_programs_parse_without_execution(self):
        compile(network.CANARY,"canary.py","exec")
        compile(network.GUEST.replace("CONFIG_JSON",repr("{}")),"guest.py","exec")

    def test_all_denied_without_positive_controls_is_not_pass(self):
        guest={"passed":True,"checks":{"host_listening_denied_port":True,"peer_listening_canary":True,
                                       "metadata_ipv4":True,"external_ipv4_direct":True}}
        with self.assertRaisesRegex(RuntimeError,"positive control"):
            network.validate_reports({"one":guest},[{"uuid":"one"}],0)
        with self.assertRaisesRegex(RuntimeError,"incomplete"):
            network.validate_reports({},[{"uuid":"one"}],0)


if __name__=="__main__":unittest.main()
