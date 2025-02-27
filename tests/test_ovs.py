import pytest

from testlib import Retis, assert_events_present


def test_ovs_sanity(two_port_ovs):
    ns = two_port_ovs[1]

    retis = Retis()

    retis.collect("-c", "ovs,skb", "-f", "icmp")
    print(ns.run("ns0", "ip", "link", "show"))
    print(ns.run("ns0", "ping", "-c", "1", "192.168.1.2"))
    retis.stop()

    events = retis.events()
    print(events)
    execs = list(
        filter(
            lambda e: e.get("kernel", {}).get("symbol")
            == "openvswitch:ovs_do_execute_action",
            events,
        )
    )

    assert len(execs) == 2


# Expected OVS upcall events.
def gen_expected_events(skb):
    return [
        # Packet hits ovs_dp_upcall. Upcall start.
        {
            "kernel": {
                "probe_type": "raw_tracepoint",
                "symbol": "openvswitch:ovs_dp_upcall",
            },
            "ovs": {"event_type": "upcall"},
            "skb": skb,
            "skb-tracking": {"orig_head": "&orig_head"},  # Store orig_head in aliases
        },
        # Packet is enqueued for upcall (only 1, i.e: no fragmentation
        # expected).
        {
            "kernel": {
                "probe_type": "kretprobe",
                "symbol": "queue_userspace_packet",
            },
            "ovs": {
                "event_type": "upcall_enqueue",
                "queue_id": "&queue_id",  # Store queue_id
            },
            "skb": skb,
            "skb-tracking": {"orig_head": "*orig_head"},  # Check same orig_head
        },
        # Upcall ends.
        {
            "kernel": {
                "probe_type": "kretprobe",
                "symbol": "ovs_dp_upcall",
            },
            "ovs": {
                "event_type": "upcall_return",
            },
            "skb": skb,
            "skb-tracking": {"orig_head": "*orig_head"},  # Check same orig_head
        },
        # Upcall is received by userspace.
        {
            "userspace": {
                "probe_type": "usdt",
                "symbol": "dpif_recv:recv_upcall",
            },
            "ovs": {
                "event_type": "recv_upcall",
                "queue_id": "*queue_id",  # Check queue_id
            },
        },
        # ovs-vswitchd puts a new flow for this packet.
        {
            "userspace": {
                "probe_type": "usdt",
                "symbol": "dpif_netlink_operate__:op_flow_put",
            },
            "ovs": {
                "event_type": "flow_operation",
                "op_type": "put",
                "queue_id": "*queue_id",  # Check queue_id
            },
        },
        # ovs-vswitchd executes the actions on this packet.
        {
            "userspace": {
                "probe_type": "usdt",
                "symbol": "dpif_netlink_operate__:op_flow_execute",
            },
            "ovs": {
                "event_type": "flow_operation",
                "op_type": "exec",
                "queue_id": "*queue_id",  # Check queue_id
            },
        },
        # Single action execution: Output.
        {
            "kernel": {
                "probe_type": "raw_tracepoint",
                "symbol": "openvswitch:ovs_do_execute_action",
            },
            "skb": skb,
            "ovs": {
                "action": "output",
                "event_type": "action_execute",
                "queue_id": "*queue_id",  # Check queue_id
            },
        },
    ]


@pytest.mark.ovs_track
def test_ovs_tracking(two_port_ovs):
    (ovs, ns) = two_port_ovs

    retis = Retis()

    # Clean stale flows if any
    ovs.appctl("dpctl/del-flows")

    # Ensure ARP tables are warm
    ns.run("ns0", "arping", "-c", "1", "192.168.1.2")

    # Start collection and test
    retis.collect("-c", "ovs,skb,skb-tracking", "-f", "ip", "--ovs-track")
    ns.run("ns0", "ping", "-c", "1", "192.168.1.2")
    retis.stop()

    events = retis.events()

    skb_icmp_req = {
        "ip": {
            "saddr": "192.168.1.1",
            "daddr": "192.168.1.2",
        },
        "icmp": {"type": 8},  # Echo Request
    }
    skb_icmp_resp = {
        "ip": {
            "saddr": "192.168.1.2",
            "daddr": "192.168.1.1",
        },
        "icmp": {"type": 0},  # Echo Reply
    }

    # Expected eventes for both directions
    expected_events = gen_expected_events(skb_icmp_req) + gen_expected_events(
        skb_icmp_resp
    )

    assert_events_present(events, expected_events)

    series = retis.sort()
    # All events from the same direction must belong to the same packet (same
    # global tracking id).
    assert len(series) == 2
    assert len(series[0]) == len(expected_events) / 2
    assert len(series[1]) == len(expected_events) / 2


@pytest.mark.ovs_track
def test_ovs_tracking_filtered(two_port_ovs):
    (ovs, ns) = two_port_ovs

    retis = Retis()

    # Clean stale flows if any
    ovs.appctl("dpctl/del-flows")

    # Not warming up ARP here so we expect some ARP traffic to flow but it
    # should be filtered out.
    retis.collect(
        "-c",
        "ovs,skb,skb-tracking",
        "-f",
        "ip src 192.168.1.1 and icmp",
        "--skb-sections",
        "eth,ip,icmp",
        "--ovs-track",
    )
    ns.run("ns0", "ping", "-c", "1", "192.168.1.2")
    retis.stop()

    events = retis.events()

    skb_icmp_req = {
        "ip": {
            "saddr": "192.168.1.1",
            "daddr": "192.168.1.2",
        },
        "icmp": {"type": 8},  # Echo Request
    }

    # We only expect one way events
    expected_events = gen_expected_events(skb_icmp_req)
    assert_events_present(events, expected_events)

    # Ensure we didn't pick up any ARP or return traffic
    return_events = filter(
        lambda e: e.get("skb", {}).get("ip", {}).get("saddr", None) == "192.168.1.2",
        events,
    )
    assert len(list(return_events)) == 0

    arps = filter(
        lambda e: e.get("skb", {}).get("eth", {}).get("etype", None) == 0x0806,
        events,
    )
    assert len(list(arps)) == 0


@pytest.mark.ovs_track
def test_ovs_filtered_userspace(two_port_ovs):
    (ovs, ns) = two_port_ovs

    retis = Retis()

    # Clean stale flows if any
    ovs.appctl("dpctl/del-flows")

    # Setting a filter that should not match any traffic.
    retis.collect(
        "-c",
        "ovs,skb,skb-tracking",
        "-f",
        "udp port 9999",
        "--ovs-track",
    )
    ns.run("ns0", "ping", "-c", "1", "192.168.1.2")
    retis.stop()

    events = retis.events()

    # Ensure we didn't pick up userspace events, i.e: all got filtered out.
    userspace = filter(lambda e: "userspace" in e, events)
    assert len(list(userspace)) == 0
