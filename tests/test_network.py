"""IP-Ermittlung und IP-Auswahl im libvirt-Netz."""

from app.config import DEFAULT_TEMPLATE_XML
from app.network import DhcpHost, DhcpState, free_ip, ip_for_macs, parse_network_xml, vm_macs

# Aufbau wie das Netz "default" auf maxwork
NET_XML = """<network>
  <name>default</name>
  <forward mode='nat'/>
  <ip address='192.168.122.1' netmask='255.255.255.0'>
    <dhcp>
      <range start='192.168.122.2' end='192.168.122.254'/>
      <host mac='52:54:00:96:4A:4D' name='kunde-a' ip='192.168.122.243'/>
      <host mac='52:54:00:cb:ad:a3' name='kunde-b' ip='192.168.122.122'/>
    </dhcp>
  </ip>
  <ip family='ipv6' address='fd00::1' prefix='64'/>
</network>"""


def test_parse_network_xml():
    st = parse_network_xml("default", NET_XML)
    assert st.gateway == "192.168.122.1"
    assert st.ranges == [("192.168.122.2", "192.168.122.254")]
    assert st.hosts[0] == DhcpHost("52:54:00:96:4a:4d", "kunde-a", "192.168.122.243")


def test_vm_macs_from_template():
    xml = DEFAULT_TEMPLATE_XML.read_text()
    assert vm_macs(xml, "default") == ["52:54:00:9a:32:98"]
    assert vm_macs(xml, "anderes-netz") == []


def test_ip_reservation_before_lease():
    st = parse_network_xml("default", NET_XML)
    st.leases = [("52:54:00:96:4a:4d", "192.168.122.50"), ("52:54:00:11:11:11", "192.168.122.60")]
    assert ip_for_macs(st, ["52:54:00:96:4A:4D"]) == "192.168.122.243"
    assert ip_for_macs(st, ["52:54:00:11:11:11"]) == "192.168.122.60"
    assert ip_for_macs(st, ["52:54:00:99:99:99"]) is None


def test_free_ip_skips_gateway_reserved_and_leases():
    st = parse_network_xml("default", NET_XML)
    assert free_ip(st) == "192.168.122.2"
    st.leases = [("x", "192.168.122.2"), ("y", "192.168.122.3")]
    st.hosts.append(DhcpHost("z", "c", "192.168.122.4"))
    assert free_ip(st) == "192.168.122.5"


def test_free_ip_none():
    st = DhcpState("default", "10.0.0.1", [("10.0.0.2", "10.0.0.3")],
                   [DhcpHost("a", "a", "10.0.0.2")], [("b", "10.0.0.3")])
    assert free_ip(st) is None
