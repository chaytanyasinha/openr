#!/usr/bin/env python3

#
# Copyright (c) 2014-present, Facebook, Inc.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.
#


import copy
import datetime
import ipaddress
import json
import sys
from builtins import chr, input, map
from collections import defaultdict
from itertools import product
from typing import Any, Dict, List, Optional, Tuple, Union

import bunch
import click
import zmq
from openr.AllocPrefix import ttypes as alloc_types
from openr.clients.kvstore_client import KvStoreClient
from openr.clients.lm_client import LMClient
from openr.Fib import ttypes as fib_types
from openr.KvStore import ttypes as kv_store_types
from openr.Lsdb import ttypes as lsdb_types
from openr.Network import ttypes as network_types
from openr.Platform import FibService, ttypes as platform_types
from openr.utils import ipnetwork, printing
from openr.utils.consts import Consts
from openr.utils.serializer import deserialize_thrift_object
from thrift.protocol import TBinaryProtocol
from thrift.transport import TSocket, TTransport


def yesno(question, skip_confirm=False):
    """
    Ask a yes/no question. No default, we want to avoid mistakes as
    much as possible. Repeat the question until we receive a valid
    answer.
    """

    if skip_confirm:
        print("Skipping interactive confirmation!")
        return True

    while True:
        try:
            prompt = "{} [yn] ".format(question)
            answer = input(prompt).lower()
        except EOFError:
            with open("/dev/tty") as sys.stdin:
                continue
        if answer in ["y", "yes"]:
            return True
        elif answer in ["n", "no"]:
            return False


def json_dumps(data):
    """
    Gives consistent formatting for JSON dumps for our CLI

    :param data: python dictionary object

    :return: json encoded string
    """

    def make_serializable(obj):
        """
        Funtion called if a non seralizable object is hit
        - Today we only support bytes to str for Python 3

        :param obj: object that can not be serializable

        :return: decode of bytes to a str
        """

        return obj.decode("utf-8")

    return json.dumps(
        data, default=make_serializable, sort_keys=True, indent=2, ensure_ascii=False
    )


def time_since(timestamp):
    """
    :param timestamp: in seconds since unix time

    :returns: difference between now and the timestamp, in a human-friendly,
              condensed format

    Example format:

    time_since(10000)
    >>> 112d11h

    :rtype: datetime.timedelta
    """
    time_since_epoch = datetime.datetime.utcnow() - datetime.datetime(
        year=1970, month=1, day=1
    )
    tdelta = time_since_epoch - datetime.timedelta(seconds=timestamp)
    d = {"days": tdelta.days}
    d["hours"], rem = divmod(tdelta.seconds, 3600)
    d["minutes"], d["seconds"] = divmod(rem, 60)
    if d["days"]:
        fmt = "{days}d{hours}h"
    elif d["hours"]:
        fmt = "{hours}h{minutes}m"
    else:
        fmt = "{minutes}m{seconds}s"
    return fmt.format(**d)


def get_fib_agent_client(
    host, port, timeout_ms, client_id=platform_types.FibClient.OPENR, service=FibService
):
    """
    Get thrift client for talking to Fib thrift service

    :param host: thrift server name or ip
    :param port: thrift server port

    :returns: The thrift client
    :rtype: FibService.Client
    """
    transport = TSocket.TSocket(host, port)
    transport.setTimeout(timeout_ms)
    transport = TTransport.TFramedTransport(transport)
    protocol = TBinaryProtocol.TBinaryProtocol(transport)
    client = service.Client(protocol)
    client.host = host  # Assign so that we can refer later on
    client.port = port  # Assign so that we can refer later on
    client.client_id = client_id  # Assign so that we can refer later on
    transport.open()
    return client


def get_connected_node_name(cli_opts):
    """ get the identity of the connected node by querying link monitor"""

    client = LMClient(cli_opts)

    try:
        return client.get_identity()
    except zmq.error.Again:
        return cli_opts.host


def get_route_nexthops(
    route: network_types.UnicastRoute
) -> List[network_types.NextHopThrift]:
    """
    DEPRECATED: this function is meant to keep backward functionality with old
    vs new way of expressing route nexthops
    """

    if route.nextHops:  # Checks for both null and empty list
        return route.nextHops

    return [network_types.NextHopThrift(address=nh) for nh in route.deprecatedNexthops]


def parse_nodes(cli_opts, nodes):
    """ parse nodes from user input

        :return set: the set of nodes
    """

    if not nodes:
        nodes = get_connected_node_name(cli_opts)
    nodes = set(nodes.strip().split(","))

    return nodes


def sprint_prefixes_db_full(prefix_db, loopback_only=False):
    """ given serialized prefixes output an array of lines
            representing those prefixes. IPV6 prefixes come before IPV4 prefixes.

        :prefix_db lsdb_types.PrefixDatabase: prefix database
        :loopback_only : is only loopback address expected

        :return [str]: the array of prefix strings
    """

    prefix_strs = []
    sorted_entries = sorted(
        sorted(prefix_db.prefixEntries, key=lambda x: x.prefix.prefixLength),
        key=lambda x: x.prefix.prefixAddress.addr,
    )
    for prefix_entry in sorted_entries:
        if loopback_only and prefix_entry.type is not lsdb_types.PrefixType.LOOPBACK:
            continue
        prefix_strs.append(
            [
                ipnetwork.sprint_prefix(prefix_entry.prefix),
                ipnetwork.sprint_prefix_type(prefix_entry.type),
                ipnetwork.sprint_prefix_forwarding_type(prefix_entry.forwardingType),
            ]
        )

    return printing.render_horizontal_table(
        prefix_strs, ["Prefix", "Client Type", "Forwarding Type"]
    )


def alloc_prefix_to_loopback_ip_str(prefix):
    """
    :param prefix: IpPrefix representing an allocation prefix (CIDR network)

    :returns: Loopback IP corresponding to allocation prefix
    :rtype: string
    """

    ip_addr = prefix.prefixAddress.addr
    print(ip_addr)
    if prefix.prefixLength != 128:
        ip_addr = ip_addr[:-1] + chr(ord(ip_addr[-1]) | 1)
    print(ip_addr)
    return ipnetwork.sprint_addr(ip_addr)


def print_prefixes_table(resp, nodes, iter_func):
    """ print prefixes """

    def _parse_prefixes(rows, prefix_db):
        if isinstance(prefix_db, kv_store_types.Value):
            prefix_db = deserialize_thrift_object(
                prefix_db.value, lsdb_types.PrefixDatabase
            )

        rows.append(
            ["{}".format(prefix_db.thisNodeName), sprint_prefixes_db_full(prefix_db)]
        )

    rows = []
    iter_func(rows, resp, nodes, _parse_prefixes)
    print(printing.render_vertical_table(rows))


def thrift_to_dict(thrift_inst, update_func=None):
    """ convert thrift instance into a dict in strings

        :param thrift_inst: a thrift instance
        :param update_func: transformation function to update dict value of
                            thrift object. It is optional.

        :return dict: dict with attributes as key, value in strings
    """

    gen_dict = copy.copy(thrift_inst).__dict__
    if update_func is not None:
        update_func(gen_dict, thrift_inst)

    return gen_dict


def prefix_entry_to_dict(prefix_entry):
    """ convert prefixEntry from thrift instance into a dict in strings """

    def _update(prefix_entry_dict, prefix_entry):
        # Only addrs need string conversion so we udpate them
        prefix_entry_dict.update(
            {
                "prefix": ipnetwork.sprint_prefix(prefix_entry.prefix),
                "data": prefix_entry.data,
            }
        )

    return thrift_to_dict(prefix_entry, _update)


def prefix_db_to_dict(prefixes_map, prefix_db):
    if isinstance(prefix_db, kv_store_types.Value):
        prefix_db = deserialize_thrift_object(
            prefix_db.value, lsdb_types.PrefixDatabase
        )

    prefixEntries = list(map(prefix_entry_to_dict, prefix_db.prefixEntries))
    prefixes_map[prefix_db.thisNodeName] = {"prefixEntries": prefixEntries}


def print_prefixes_json(resp, nodes, iter_func):
    """ print prefixes in json """

    prefixes_map = {}
    iter_func(prefixes_map, resp, nodes, prefix_db_to_dict)
    print(json_dumps(prefixes_map))


def update_global_adj_db(global_adj_db, adj_db):
    """ update the global adj map based on publication from single node

        :param global_adj_map map(node, AdjacencyDatabase)
            the map for all adjacencies in the network - to be updated
        :param adj_db lsdb_types.AdjacencyDatabase: publication from single
            node
    """

    assert isinstance(adj_db, lsdb_types.AdjacencyDatabase)

    global_adj_db[adj_db.thisNodeName] = adj_db


def build_global_adj_db(resp):
    """ build a map of all adjacencies in the network. this is used
        for bi-directional validation

        :param resp kv_store_types.Publication: the parsed publication

        :return map(node, AdjacencyDatabase): the global
            adj map, devices name mapped to devices it connects to, and
            properties of that connection
    """

    # map: (node) -> AdjacencyDatabase)
    global_adj_db = {}

    for (key, value) in resp.keyVals.items():
        if not key.startswith(Consts.ADJ_DB_MARKER):
            continue
        adj_db = deserialize_thrift_object(value.value, lsdb_types.AdjacencyDatabase)
        update_global_adj_db(global_adj_db, adj_db)

    return global_adj_db


def build_global_prefix_db(resp):
    """ build a map of all prefixes in the network. this is used
        for checking for changes in topology

        :param resp kv_store_types.Publication: the parsed publication

        :return map(node, set([prefix])): the global prefix map,
            prefixes mapped to the node
    """

    # map: (node) -> set([prefix])
    global_prefix_db = {}

    for (key, value) in resp.keyVals.items():
        if not key.startswith(Consts.PREFIX_DB_MARKER):
            continue
        prefix_db = deserialize_thrift_object(value.value, lsdb_types.PrefixDatabase)
        update_global_prefix_db(global_prefix_db, prefix_db)

    return global_prefix_db


def dump_adj_db_full(global_adj_db, adj_db, bidir):
    """ given an adjacency database, dump neighbors. Use the
            global adj database to validate bi-dir adjacencies

        :param global_adj_db map(str, AdjacencyDatabase):
            map of node names to their adjacent node names
        :param adj_db lsdb_types.AdjacencyDatabase: latest from kv store
        :param bidir bool: only dump bidir adjacencies

        :return (nodeLabel, [adjacencies]): tuple of node label and list
            of adjacencies
    """

    assert isinstance(adj_db, lsdb_types.AdjacencyDatabase)
    this_node_name = adj_db.thisNodeName

    if not bidir:
        return (adj_db.nodeLabel, adj_db.isOverloaded, adj_db.adjacencies)

    adjacencies = []

    for adj in adj_db.adjacencies:
        other_node_db = global_adj_db.get(adj.otherNodeName, None)
        if other_node_db is None:
            continue
        other_node_neighbors = {
            (a.otherNodeName, a.otherIfName) for a in other_node_db.adjacencies
        }
        if (this_node_name, adj.ifName) not in other_node_neighbors:
            continue
        adjacencies.append(adj)

    return (adj_db.nodeLabel, adj_db.isOverloaded, adjacencies)


def adj_to_dict(adj):
    """ convert adjacency from thrift instance into a dict in strings """

    def _update(adj_dict, adj):
        # Only addrs need string conversion so we udpate them
        adj_dict.update(
            {
                "nextHopV6": ipnetwork.sprint_addr(adj.nextHopV6.addr),
                "nextHopV4": ipnetwork.sprint_addr(adj.nextHopV4.addr),
            }
        )

    return thrift_to_dict(adj, _update)


def adj_db_to_dict(adjs_map, adj_dbs, adj_db, bidir, version):
    """ convert adj db to dict """

    node_label, is_overloaded, adjacencies = dump_adj_db_full(adj_dbs, adj_db, bidir)

    if not adjacencies:
        return

    adjacencies = list(map(adj_to_dict, adjacencies))

    # Dump is keyed by node name with attrs as key values
    adjs_map[adj_db.thisNodeName] = {
        "node_label": node_label,
        "overloaded": is_overloaded,
        "adjacencies": adjacencies,
    }
    if version:
        adjs_map[adj_db.thisNodeName]["version"] = version


def adj_dbs_to_dict(resp, nodes, bidir, iter_func):
    """ get parsed adjacency db

        :param resp kv_store_types.Publication, or decision_types.adjDbs
        :param nodes set: the set of the nodes to print prefixes for
        :param bidir bool: only dump bidirectional adjacencies

        :return map(node, map(adjacency_keys, (adjacency_values)): the parsed
            adjacency DB in a map with keys and values in strings
    """
    adj_dbs = resp
    if isinstance(adj_dbs, kv_store_types.Publication):
        adj_dbs = build_global_adj_db(resp)

    def _parse_adj(adjs_map, adj_db):
        version = None
        if isinstance(adj_db, kv_store_types.Value):
            version = adj_db.version
            adj_db = deserialize_thrift_object(
                adj_db.value, lsdb_types.AdjacencyDatabase
            )
        adj_db_to_dict(adjs_map, adj_dbs, adj_db, bidir, version)

    adjs_map = {}
    iter_func(adjs_map, resp, nodes, _parse_adj)
    return adjs_map


def print_json(map):
    """ print json format of input dict

        @map: list of dict
    """

    print(json_dumps(map))


def print_adjs_table(adjs_map, enable_color, neigh=None, interface=None):
    """ print adjacencies

        :param adjacencies as list of dict
    """

    column_labels = [
        "Neighbor",
        "Local Intf",
        "Remote Intf",
        "Metric",
        "Label",
        "NextHop-v4",
        "NextHop-v6",
        "Uptime",
    ]

    output = []
    adj_found = False
    for node, val in sorted(adjs_map.items()):
        adj_tokens = []

        # report adjacency version
        if "version" in val:
            adj_tokens.append("Version: {}".format(val["version"]))

        # report overloaded only when it is overloaded
        is_overloaded = val["overloaded"]
        if is_overloaded:
            overload_str = "{}".format(is_overloaded)
            if enable_color:
                overload_str = click.style(overload_str, fg="red")
            adj_tokens.append("Overloaded: {}".format(overload_str))

        # report node label if non zero
        node_label = val["node_label"]
        if node_label:
            adj_tokens.append("Node Label: {}".format(node_label))

        # horizontal adj table for a node
        rows = []
        seg = ""
        for adj in sorted(val["adjacencies"], key=lambda adj: adj["otherNodeName"]):
            # filter if set
            if neigh is not None and interface is not None:
                if neigh == adj["otherNodeName"] and interface == adj["ifName"]:
                    adj_found = True
                else:
                    continue

            overload_status = click.style("Overloaded", fg="red")
            metric = (
                (overload_status if enable_color else "OVERLOADED")
                if adj["isOverloaded"]
                else adj["metric"]
            )
            uptime = time_since(adj["timestamp"]) if adj["timestamp"] else ""

            rows.append(
                [
                    adj["otherNodeName"],
                    adj["ifName"],
                    adj["otherIfName"],
                    metric,
                    adj["adjLabel"],
                    adj["nextHopV4"],
                    adj["nextHopV6"],
                    uptime,
                ]
            )
            seg = printing.render_horizontal_table(
                rows, column_labels, tablefmt="plain"
            )
        cap = "{} {} {}".format(node, "=>" if adj_tokens else "", ", ".join(adj_tokens))
        output.append([cap, seg])

    if neigh is not None and interface is not None and not adj_found:
        print("Adjacency with {} {} is not formed.".format(neigh, interface))
        return

    print(printing.render_vertical_table(output))


def sprint_adj_db_full(global_adj_db, adj_db, bidir):
    """ given serialized adjacency database, print neighbors. Use the
            global adj database to validate bi-dir adjacencies

        :param global_adj_db map(str, AdjacencyDatabase):
            map of node names to their adjacent node names
        :param adj_db lsdb_types.AdjacencyDatabase: latest from kv store
        :param bidir bool: only print bidir adjacencies

        :return [str]: list of string to be printed
    """

    assert isinstance(adj_db, lsdb_types.AdjacencyDatabase)
    this_node_name = adj_db.thisNodeName
    node_label_str = "Node Label: {}".format(adj_db.nodeLabel)

    rows = []

    column_labels = [
        "Neighbor",
        "Local Intf",
        "Remote Intf",
        "Metric",
        "Label",
        "NextHop-v4",
        "NextHop-v6",
        "Uptime",
    ]

    for adj in adj_db.adjacencies:
        if bidir:
            other_node_db = global_adj_db.get(adj.otherNodeName, None)
            if other_node_db is None:
                continue
            other_node_neighbors = {a.otherNodeName for a in other_node_db.adjacencies}
            if this_node_name not in other_node_neighbors:
                continue

        nh_v6 = ipnetwork.sprint_addr(adj.nextHopV6.addr)
        nh_v4 = ipnetwork.sprint_addr(adj.nextHopV4.addr)
        overload_status = click.style("Overloaded", fg="red")
        metric = overload_status if adj.isOverloaded else adj.metric
        uptime = time_since(adj.timestamp) if adj.timestamp else ""

        rows.append(
            [
                adj.otherNodeName,
                adj.ifName,
                adj.otherIfName,
                metric,
                adj.adjLabel,
                nh_v4,
                nh_v6,
                uptime,
            ]
        )

    return node_label_str, printing.render_horizontal_table(rows, column_labels)


def interface_db_to_dict(value):
    """
    Convert a thrift::Value representation of InterfaceDatabase to bunch
    object
    """

    def _parse_intf_info(info):
        addrs = []
        if info.networks is not None:
            addrs = [ipnetwork.sprint_addr(v.prefixAddress.addr) for v in info.networks]
        else:
            addrs = [ipnetwork.sprint_addr(v.addr) for v in info.v4Addrs] + [
                ipnetwork.sprint_addr(v.addr) for v in info.v6LinkLocalAddrs
            ]

        return bunch.Bunch(
            **{"isUp": info.isUp, "ifIndex": info.ifIndex, "Addrs": addrs}
        )

    assert isinstance(value, kv_store_types.Value)
    intf_db = deserialize_thrift_object(value.value, lsdb_types.InterfaceDatabase)
    return bunch.Bunch(
        **{
            "thisNodeName": intf_db.thisNodeName,
            "interfaces": {
                k: _parse_intf_info(v) for k, v in intf_db.interfaces.items()
            },
        }
    )


def interface_dbs_to_dict(publication, nodes, iter_func):
    """ get parsed interface dbs

        :param publication kv_store_types.Publication
        :param nodes set: the set of the nodes to filter interfaces for

        :return map(node, InterfaceDatabase.bunch): the parsed
            adjacency DB in a map with keys and values in strings
    """

    assert isinstance(publication, kv_store_types.Publication)

    def _parse_intf_db(intf_map, value):
        intf_db = interface_db_to_dict(value)
        intf_map[intf_db.thisNodeName] = intf_db

    intf_dbs_map = {}
    iter_func(intf_dbs_map, publication, nodes, _parse_intf_db)
    return intf_dbs_map


def next_hop_thrift_to_dict(nextHop: network_types.NextHopThrift) -> Dict[str, Any]:
    """ convert nextHop from thrift instance into a dict in strings """

    def _update(next_hop_dict, nextHop):
        next_hop_dict.update(
            {
                "address": ipnetwork.sprint_addr(nextHop.address.addr),
                "nextHop": ipnetwork.sprint_addr(nextHop.address.addr),
                "ifName": nextHop.address.ifName,
            }
        )
        if nextHop.mplsAction:
            next_hop_dict.update({"mplsAction": thrift_to_dict(nextHop.mplsAction)})

    return thrift_to_dict(nextHop, _update)


def unicast_route_to_dict(route):
    """ convert route from thrift instance into a dict in strings """

    def _update(route_dict, route):
        route_dict.update(
            {
                "dest": ipnetwork.sprint_prefix(route.dest),
                "nextHops": [next_hop_thrift_to_dict(nh) for nh in route.nextHops],
            }
        )

    return thrift_to_dict(route, _update)


def mpls_route_to_dict(route: network_types.MplsRoute) -> Dict[str, Any]:
    """
    Convert MPLS route to json serializable dict object
    """

    def _update(route_dict, route: network_types.MplsRoute):
        route_dict.update(
            {"nextHops": [next_hop_thrift_to_dict(nh) for nh in route.nextHops]}
        )

    return thrift_to_dict(route, _update)


def route_db_to_dict(route_db: fib_types.RouteDatabase) -> Dict[str, Any]:
    """
    Convert route from thrift instance into a dict in strings
    """

    ret = {
        "unicastRoutes": [unicast_route_to_dict(r) for r in route_db.unicastRoutes],
        "mplsRoutes": [mpls_route_to_dict(r) for r in route_db.mplsRoutes],
    }
    return ret


def print_routes_json(
    route_db_dict, prefixes: List[str] = None, labels: List[int] = None
):
    """
    Print json representation of routes. Takes prefixes and labels to
    filter
    """

    networks = None
    if prefixes:
        networks = [ipaddress.ip_network(p) for p in prefixes]

    # Filter out all routes based on prefixes and labels
    for routes in route_db_dict.values():
        filtered_unicast_routes = []
        for route in routes["unicastRoutes"]:
            if labels or networks:
                if networks and ipnetwork.contain_any_prefix(route["dest"], networks):
                    filtered_unicast_routes.append(route)
            else:
                filtered_unicast_routes.append(route)
        routes["unicastRoutes"] = filtered_unicast_routes

        filtered_mpls_routes = []
        for route in routes["mplsRoutes"]:
            if labels or prefixes:
                if labels and int(route["topLabel"]) in labels:
                    filtered_mpls_routes.append(route)
            else:
                filtered_mpls_routes.append(route)
        routes["mplsRoutes"] = filtered_mpls_routes

    # Filter

    print(json_dumps(route_db_dict))


def print_route_db(
    route_db: fib_types.RouteDatabase,
    prefixes: List[str] = None,
    labels: List[int] = None,
) -> None:
    """ print the the routes from Decision/Fib module """

    if prefixes or not labels:
        print_unicast_routes(
            "Unicast Routes for {}".format(route_db.thisNodeName),
            route_db.unicastRoutes,
            prefixes=prefixes,
        )
    if labels or not prefixes:
        print_mpls_routes(
            "MPLS Routes for {}".format(route_db.thisNodeName),
            route_db.mplsRoutes,
            labels=labels,
        )


def find_adj_list_deltas(old_adj_list, new_adj_list, tags=None):
    """ given the old adj list and the new one for some node, return
        change list.

        :param old_adj_list [Adjacency]: old adjacency list
        :param new_adj_list [Adjacency]: new adjacency list
        :param tags 3-tuple(string): a tuple of labels for
            (in old only, in new only, in both but different)

        :return [(str, Adjacency, Adjacency)]: list of tuples of
            (changeType, oldAdjacency, newAdjacency)
            in the case where an adjacency is added or removed,
            oldAdjacency or newAdjacency is None, respectively
    """
    if not tags:
        tags = ("NEIGHBOR_DOWN", "NEIGHBOR_UP", "NEIGHBOR_UPDATE")

    old_neighbors = {(a.otherNodeName, a.ifName) for a in old_adj_list}
    new_neighbors = {(a.otherNodeName, a.ifName) for a in new_adj_list}
    delta_list = [
        (tags[0], a, None)
        for a in old_adj_list
        if (a.otherNodeName, a.ifName) in old_neighbors - new_neighbors
    ]
    delta_list.extend(
        [
            (tags[1], None, a)
            for a in new_adj_list
            if (a.otherNodeName, a.ifName) in new_neighbors - old_neighbors
        ]
    )
    delta_list.extend(
        [
            (tags[2], a, b)
            for a, b in product(old_adj_list, new_adj_list)
            if (
                a.otherNodeName == b.otherNodeName
                and a.ifName == b.ifName
                and (a.otherNodeName, a.ifName) in new_neighbors & old_neighbors
                and a != b
            )
        ]
    )
    return delta_list


def adj_list_deltas_json(adj_deltas_list, tags=None):
    """
    Parses a list of adjacency list deltas (from func find_adj_list_deltas),
    and returns the data as a json-formatted dict, and a status code.
        {
            tag-down: [nodes_down],
            tag-up: [nodes_up],
            tag-update: [
                {
                    "old_adj": old_adj,
                    "new_adj": new_adj
                }
            ]
        }

    @param adj_deltas_list: list<(changeType, oldAdjacency, newAdjacency)>
    @param tags: 3-tuple(string). a tuple of labels for
        (in old only, in new only, in both but different)
    """
    if not tags:
        tags = "NEIGHBOR_DOWN, NEIGHBOR_UP, NEIGHBOR_UPDATE"

    return_code = 0
    nodes_down = []
    nodes_up = []
    nodes_update = []

    for data in adj_deltas_list:
        old_adj = adjacency_to_dict(data[1]) if data[1] else None
        new_adj = adjacency_to_dict(data[2]) if data[2] else None

        if data[0] == tags[0]:
            assert new_adj is None
            nodes_down.append(old_adj)
            return_code = 1
        elif data[0] == tags[1]:
            assert old_adj is None
            nodes_up.append(new_adj)
            return_code = 1
        elif data[0] == tags[2]:
            assert old_adj is not None and new_adj is not None
            nodes_update.append({tags[0]: old_adj, tags[1]: new_adj})
            return_code = 1
        else:
            raise ValueError(
                'Unexpected change type "{}" in adjacency deltas list'.format(data[0])
            )

    deltas_json = {}

    if nodes_down:
        deltas_json.update({tags[0]: nodes_down})
    if nodes_up:
        deltas_json.update({tags[1]: nodes_up})
    if nodes_update:
        deltas_json.update({tags[2]: nodes_update})

    return deltas_json, return_code


def adjacency_to_dict(adjacency):
    """ convert adjacency from thrift instance into a dict in strings

        :param adjacency as a thrift instance: adjacency

        :return dict: dict with adjacency attributes as key, value in strings
    """

    # Only addrs need string conversion so we udpate them
    adj_dict = copy.copy(adjacency).__dict__
    adj_dict.update(
        {
            "nextHopV6": ipnetwork.sprint_addr(adjacency.nextHopV6.addr),
            "nextHopV4": ipnetwork.sprint_addr(adjacency.nextHopV4.addr),
        }
    )

    return adj_dict


def sprint_adj_delta(old_adj, new_adj):
    """ given old and new adjacency, create a list of strings that summarize
        changes. If oldAdj is None, this function prints all attridutes of
        newAdj

        :param oldAdj Adjacency: can be None
        :param newAdj Adjacency: new

        :return str: table summarizing the change
    """
    assert new_adj is not None
    rows = []
    new_adj_dict = adjacency_to_dict(new_adj)
    if old_adj is not None:
        old_adj_dict = adjacency_to_dict(old_adj)
        for k in sorted(new_adj_dict.keys()):
            if old_adj_dict.get(k) != new_adj_dict.get(k):
                rows.append([k, old_adj_dict.get(k), "-->", new_adj_dict.get(k)])
    else:
        for k in sorted(new_adj_dict.keys()):
            rows.append([k, new_adj_dict[k]])
    return printing.render_horizontal_table(rows)


def sprint_pub_update(global_publication_db, key, value):
    """
    store new version and originatorId for a key in the global_publication_db
    return a string summarizing any changes in a publication from kv store
    """

    rows = []
    old_version, old_originator_id = global_publication_db.get(key, (None, None))

    if old_version != value.version:
        rows.append(["version:", old_version, "-->", value.version])
    if old_originator_id != value.originatorId:
        rows.append(["originatorId:", old_originator_id, "-->", value.originatorId])
    ttl = "INF" if value.ttl == Consts.CONST_TTL_INF else value.ttl
    rows.append(["ttlVersion:", "", "-->", value.ttlVersion])
    rows.append(["ttl:", "", "-->", ttl])
    global_publication_db[key] = (value.version, value.originatorId)
    return printing.render_horizontal_table(rows, tablefmt="plain") if rows else ""


def update_global_prefix_db(global_prefix_db, prefix_db):
    """ update the global prefix map with a single publication

        :param global_prefix_map map(node, set([str])): map of all prefixes
            in the network
        :param prefix_db lsdb_types.PrefixDatabase: publication from single
            node
    """

    assert isinstance(prefix_db, lsdb_types.PrefixDatabase)

    this_node = prefix_db.thisNodeName

    prefix_set = set()
    for prefix_entry in prefix_db.prefixEntries:
        addr_str = ipnetwork.sprint_addr(prefix_entry.prefix.prefixAddress.addr)
        prefix_len = prefix_entry.prefix.prefixLength
        prefix_set.add("{}/{}".format(addr_str, prefix_len))

    global_prefix_db[this_node] = prefix_set

    return


def sprint_adj_db_delta(new_adj_db, old_adj_db):
    """ given serialized adjacency database, print neighbors delta as
            compared to the supplied global state

        :param new_adj_db lsdb_types.AdjacencyDatabase: latest from kv store
        :param old_adj_db lsdb_types.AdjacencyDatabase: last one we had

        :return [str]: list of string to be printed
    """

    # check for deltas between old and new
    # first check for changes in the adjacencies lists
    adj_list_deltas = find_adj_list_deltas(
        old_adj_db.adjacencies, new_adj_db.adjacencies
    )

    strs = []

    for change_type, old_adj, new_adj in adj_list_deltas:
        if change_type == "NEIGHBOR_DOWN":
            strs.append(
                "{}: {} via {}".format(
                    change_type, old_adj.otherNodeName, old_adj.ifName
                )
            )
        if change_type == "NEIGHBOR_UP" or change_type == "NEIGHBOR_UPDATE":
            strs.append(
                "{}: {} via {}\n{}".format(
                    change_type,
                    new_adj.otherNodeName,
                    new_adj.ifName,
                    sprint_adj_delta(old_adj, new_adj),
                )
            )

    # check for other adjDB changes
    old_db_dict = copy.copy(old_adj_db).__dict__
    old_db_dict.pop("adjacencies", None)
    old_db_dict.pop("perfEvents", None)
    new_db_dict = copy.copy(new_adj_db).__dict__
    new_db_dict.pop("adjacencies", None)
    new_db_dict.pop("perfEvents", None)
    if new_db_dict != old_db_dict:
        rows = []
        strs.append("ADJ_DB_UPDATE: {}".format(new_adj_db.thisNodeName))
        for k in sorted(new_db_dict.keys()):
            if old_db_dict.get(k) != new_db_dict.get(k):
                rows.append([k, old_db_dict.get(k), "-->", new_db_dict.get(k)])
        strs.append(printing.render_horizontal_table(rows, tablefmt="plain"))

    return strs


def sprint_prefixes_db_delta(global_prefixes_db, prefix_db):
    """ given serialzied prefixes for a single node, output the delta
            between those prefixes and global prefixes snapshot

        :global_prefixes_db map(node, set([str])): global prefixes
        :prefix_db lsdb_types.PrefixDatabase: latest from kv store

        :return [str]: the array of prefix strings
    """

    this_node_name = prefix_db.thisNodeName
    prev_prefixes = global_prefixes_db.get(this_node_name, set())

    cur_prefixes = set()
    for prefix_entry in prefix_db.prefixEntries:
        cur_prefixes.add(ipnetwork.sprint_prefix(prefix_entry.prefix))

    added_prefixes = cur_prefixes - prev_prefixes
    removed_prefixes = prev_prefixes - cur_prefixes

    strs = ["+ {}".format(prefix) for prefix in added_prefixes]
    strs.extend(["- {}".format(prefix) for prefix in removed_prefixes])

    return strs


def dump_node_kvs(cli_opts, host):
    client = KvStoreClient(cli_opts, host=host)
    try:
        kv = client.dump_all_with_filter()
    except zmq.error.Again:
        print("cannot connect to {}'s kvstore".format(host))
        return None
    return kv


def print_allocations_table(alloc_str):
    """ print static allocations """

    rows = []
    allocations = deserialize_thrift_object(alloc_str, alloc_types.StaticAllocation)
    for node, prefix in allocations.nodePrefixes.items():
        rows.append([node, ipnetwork.sprint_prefix(prefix)])
    print(printing.render_horizontal_table(rows, ["Node", "Prefix"]))


def build_routes(prefixes, nexthops):
    """
    :param prefixes: List of prefixes in string representation
    :param nexthops: List of nexthops ip addresses in string presentation

    :returns: list network_types.UnicastRoute (structured routes)
    :rtype: list
    """

    prefixes = [ipnetwork.ip_str_to_prefix(p) for p in prefixes]
    nhs = []
    for nh_iface in nexthops:
        iface, addr = None, None
        # Nexthop may or may not be link-local. Handle it here well
        if "@" in nh_iface:
            addr, iface = nh_iface.split("@")
        elif "%" in nh_iface:
            addr, iface = nh_iface.split("%")
        else:
            addr = nh_iface
        nexthop = ipnetwork.ip_str_to_addr(addr)
        nexthop.ifName = iface
        nhs.append(nexthop)
    return [
        network_types.UnicastRoute(
            dest=p,
            deprecatedNexthops=nhs,
            nextHops=[network_types.NextHopThrift(address=nh) for nh in nhs],
        )
        for p in prefixes
    ]


def get_route_as_dict(routes):
    """
    Convert a routeDb into a dict representing routes in str format

    :param routes: list network_types.UnicastRoute (structured routes)

    :returns: dict of routes {prefix: [nexthops]}
    :rtype: dict
    """

    # Thrift object instances do not have hash support
    # Make custom stringified object so we can hash and diff
    # dict of prefixes(str) : nexthops(str)
    routes_dict = {
        ipnetwork.sprint_prefix(route.dest): sorted(
            ip_nexthop_to_str(nh, True) for nh in get_route_nexthops(route)
        )
        for route in routes
    }

    return routes_dict


def routes_difference(lhs, rhs):
    """
    Get routeDb delta between provided inputs

    :param lhs: list network_types.UnicastRoute (structured routes)
    :param rhs: list network_types.UnicastRoute (structured routes)

    :returns: list network_types.UnicastRoute (structured routes)
    :rtype: list
    """

    diff = []

    # dict of prefixes(str) : nexthops(str)
    _lhs = get_route_as_dict(lhs)
    _rhs = get_route_as_dict(rhs)

    diff_prefixes = set(_lhs) - set(_rhs)

    for prefix in diff_prefixes:
        diff.extend(build_routes([prefix], _lhs[prefix]))

    return diff


def prefixes_with_different_nexthops(lhs, rhs):
    """
    Get prefixes common to both routeDbs with different nexthops

    :param lhs: list network_types.UnicastRoute (structured routes)
    :param rhs: list network_types.UnicastRoute (structured routes)

    :returns: list str of IpPrefix common to lhs and rhs but
              have different nexthops
    :rtype: list
    """

    prefixes = []

    # dict of prefixes(str) : nexthops(str)
    _lhs = get_route_as_dict(lhs)
    _rhs = get_route_as_dict(rhs)
    common_prefixes = set(_lhs) & set(_rhs)

    for prefix in common_prefixes:
        if _lhs[prefix] != _rhs[prefix]:
            prefixes.append((prefix, _lhs[prefix], _rhs[prefix]))

    return prefixes


def compare_route_db(routes_a, routes_b, sources, enable_color, quiet=False):

    extra_routes_in_a = routes_difference(routes_a, routes_b)
    extra_routes_in_b = routes_difference(routes_b, routes_a)
    diff_prefixes = prefixes_with_different_nexthops(routes_a, routes_b)

    # return error type
    error_msg = []

    # if all good, then return early
    if not extra_routes_in_a and not extra_routes_in_b and not diff_prefixes:
        if not quiet:
            if enable_color:
                click.echo(click.style("PASS", bg="green", fg="black"))
            else:
                click.echo("PASS")
            print("{} and {} routing table match".format(*sources))
        return True, error_msg

    # Something failed.. report it
    if not quiet:
        if enable_color:
            click.echo(click.style("FAIL", bg="red", fg="black"))
        else:
            click.echo("FAIL")
        print("{} and {} routing table do not match".format(*sources))
    if extra_routes_in_a:
        caption = "Routes in {} but not in {}".format(*sources)
        if not quiet:
            print_unicast_routes(caption, extra_routes_in_a)
        else:
            error_msg.append(caption)

    if extra_routes_in_b:
        caption = "Routes in {} but not in {}".format(*reversed(sources))
        if not quiet:
            print_unicast_routes(caption, extra_routes_in_b)
        else:
            error_msg.append(caption)

    if diff_prefixes:
        caption = "Prefixes have different nexthops in {} and {}".format(*sources)
        rows = []
        for prefix, lhs_nexthops, rhs_nexthops in diff_prefixes:
            rows.append([prefix, ", ".join(lhs_nexthops), ", ".join(rhs_nexthops)])
        column_labels = ["Prefix"] + sources
        if not quiet:
            print(
                printing.render_horizontal_table(rows, column_labels, caption=caption)
            )
        else:
            error_msg.append(caption)
    return False, error_msg


def validate_route_nexthops(routes, interfaces, sources, enable_color, quiet=False):
    """
    Validate between fib routes and lm interfaces

    :param routes: list network_types.UnicastRoute (structured routes)
    :param interfaces: dict<interface-name, InterfaceDetail>
    """

    # record invalid routes in dict<error, list<route_db>>
    invalid_routes = defaultdict(list)

    # define error types
    MISSING_NEXTHOP = "Nexthop does not exist"
    INVALID_SUBNET = "Nexthop address is not in the same subnet as interface"
    INVALID_LINK_LOCAL = "Nexthop address is not link local"

    # return error type
    error_msg = []

    for route in routes:
        dest = ipnetwork.sprint_prefix(route.dest)
        # record invalid nexthops in dict<error, list<nexthops>>
        invalid_nexthop = defaultdict(list)
        for nextHop in get_route_nexthops(route):
            nh = nextHop.address
            if nh.ifName not in interfaces or not interfaces[nh.ifName].info.isUp:
                invalid_nexthop[MISSING_NEXTHOP].append(ip_nexthop_to_str(nextHop))
                continue
            # if nexthop addr is v4, make sure it belongs to same subnets as
            # interface addr
            if ipnetwork.ip_version(nh.addr) == 4:
                networks = interfaces[nh.ifName].info.networks
                if networks is None:
                    # maintain backward compatbility
                    networks = []
                for prefix in networks:
                    if ipnetwork.ip_version(
                        prefix.prefixAddress.addr
                    ) == 4 and not ipnetwork.is_same_subnet(
                        nh.addr, prefix.prefixAddress.addr, "31"
                    ):
                        invalid_nexthop[INVALID_SUBNET].append(
                            ip_nexthop_to_str(nextHop)
                        )
            # if nexthop addr is v6, make sure it's a link local addr
            elif ipnetwork.ip_version(nh.addr) == 6 and not ipnetwork.is_link_local(
                nh.addr
            ):
                invalid_nexthop[INVALID_LINK_LOCAL].append(ip_nexthop_to_str(nextHop))

        # build routes per error type
        for k, v in invalid_nexthop.items():
            invalid_routes[k].extend(build_routes([dest], v))

    # if all good, then return early
    if not invalid_routes:
        if not quiet:
            if enable_color:
                click.echo(click.style("PASS", bg="green", fg="black"))
            else:
                click.echo("PASS")
            print("Route validation successful")
        return True, error_msg

    # Something failed.. report it
    if not quiet:
        if enable_color:
            click.echo(click.style("FAIL", bg="red", fg="black"))
        else:
            click.echo("FAIL")
        print("Route validation failed")
    # Output report per error type
    for err, route_db in invalid_routes.items():
        caption = "Error: {}".format(err)
        if not quiet:
            print_unicast_routes(caption, route_db)
        else:
            error_msg.append(caption)

    return False, error_msg


def mpls_action_to_str(mpls_action: network_types.MplsAction) -> str:
    """
    Convert Network.MplsAction to string representation
    """

    action_str = network_types.MplsActionCode._VALUES_TO_NAMES.get(
        mpls_action.action, ""
    )
    label_str = ""
    if mpls_action.swapLabel is not None:
        label_str = str(mpls_action.swapLabel)
    if mpls_action.pushLabels is not None:
        label_str = "/".join(str(l) for l in mpls_action.pushLabels)
    return "mpls {} {}".format(action_str, label_str)


def ip_nexthop_to_str(
    nextHop: network_types.NextHopThrift, ignore_v4_iface: bool = False
) -> str:
    """
    Convert Network.BinaryAddress to string representation of a nexthop
    """

    nh = nextHop.address
    ifName = "%{}".format(nh.ifName) if nh.ifName else ""
    if len(nh.addr) == 4 and ignore_v4_iface:
        ifName = ""

    mpls_action_str = (
        "{} ".format(mpls_action_to_str(nextHop.mplsAction))
        if nextHop.mplsAction is not None
        else ""
    )

    return "{}{}{}".format(mpls_action_str, ipnetwork.sprint_addr(nh.addr), ifName)


def print_unicast_routes(
    caption: str,
    unicast_routes: List[network_types.UnicastRoute],
    prefixes: List[str] = None,
):
    """
    Print unicast routes. Subset specified by prefixes will be printed if specified
    """

    networks = None
    if prefixes:
        networks = [ipaddress.ip_network(p) for p in prefixes]

    route_strs = []
    for route in unicast_routes:
        entry = build_unicast_route(route, filter_for_networks=networks)
        if entry:
            dest, nexthops = entry
            paths_str = "\n".join(["via {}".format(nh) for nh in nexthops])
            route_strs.append([dest, paths_str])

    print(printing.render_vertical_table(route_strs, caption=caption))


def build_unicast_route(
    route: object,
    filter_for_networks: Optional[
        List[Union[ipaddress.IPv4Network, ipaddress.IPv6Network]]
    ] = None,
) -> Tuple[str, List[str]]:
    dest = ipnetwork.sprint_prefix(route.dest)
    if filter_for_networks and not ipnetwork.contain_any_prefix(
        dest, filter_for_networks
    ):
        return None
    nexthops = [ip_nexthop_to_str(nh) for nh in get_route_nexthops(route)]
    return dest, nexthops


def print_mpls_routes(
    caption: str, mpls_routes: List[network_types.MplsRoute], labels: List[int] = None
):
    """
    List mpls routes. Subset specified by labels will be printed if specified
    """

    route_strs = []
    for route in mpls_routes:
        if labels and route.topLabel not in labels:
            continue

        paths_str = "\n".join(
            ["via {}".format(ip_nexthop_to_str(nh)) for nh in route.nextHops]
        )
        route_strs.append([str(route.topLabel), paths_str])

    print(printing.render_vertical_table(route_strs, caption=caption))


def get_routes_json(host, client, routes, prefixes=None):

    networks = None
    if prefixes:
        networks = [ipaddress.ip_network(p) for p in prefixes]

    data = {"host": host, "client": client, "routes": []}

    for route in routes:
        dest = ipnetwork.sprint_prefix(route.dest)
        if not ipnetwork.contain_any_prefix(dest, networks):
            continue
        route_data = {
            "dest": dest,
            "nexthops": [ip_nexthop_to_str(nh) for nh in get_route_nexthops(route)],
        }
        data["routes"].append(route_data)

    return data


def get_shortest_routes(route_db):
    """
    Find all shortest routes for each prefix in routeDb

    :param route_db: RouteDatabase
    :return list of UnicastRoute of prefix & corresponding shortest nexthops
    """

    shortest_routes = []
    for route in sorted(
        route_db.unicastRoutes, key=lambda x: x.dest.prefixAddress.addr
    ):
        if not route.nextHops:
            continue

        min_metric = min(route.nextHops, key=lambda x: x.metric).metric
        nextHops = [nh for nh in route.nextHops if nh.metric == min_metric]
        shortest_routes.append(
            network_types.UnicastRoute(
                dest=route.dest,
                deprecatedNexthops=[nh.address for nh in nextHops],
                nextHops=nextHops,
            )
        )

    return shortest_routes


def print_spt_infos(spt_infos: kv_store_types.SptInfos, roots: List[str]) -> None:
    """
    print spanning tree information
    """

    output = []

    # step1. print neighbor level counters
    caption = "Neighbor DUAL Counters"
    column_labels = ["Neighbor", "Pkt(Tx/Rx)", "Msg(Tx/Rx)"]
    neighbor_counters = spt_infos.counters.neighborCounters
    rows = []
    for nb, counters in neighbor_counters.items():
        rows.append(
            [
                nb,
                "{}/{}".format(counters.pktSent, counters.pktRecv),
                "{}/{}".format(counters.msgSent, counters.msgRecv),
            ]
        )
    seg = printing.render_horizontal_table(rows, column_labels, tablefmt="plain")
    output.append([caption, seg])

    # step2. print root level counters
    root_counters = spt_infos.counters.rootCounters
    column_labels = [
        "Neighbor",
        "Query(Tx/Rx)",
        "Reply(Tx/Rx)",
        "Update(Tx/Rx)",
        "Total(Tx/Rx)",
    ]
    for root, info in spt_infos.infos.items():
        if roots is not None and root not in roots:
            continue
        if info.passive:
            state = click.style("PASSIVE", fg="green")
        else:
            state = click.style("ACTIVE", fg="red")
        cap = "root@{}[{}]: parent: {}, cost: {}, ({})children: {}".format(
            root,
            state,
            info.parent,
            info.cost,
            len(info.children),
            ",".join(info.children),
        )
        rows = []
        for nb, counters in root_counters.get(root).items():
            rows.append(
                [
                    nb,
                    "{}/{}".format(counters.querySent, counters.queryRecv),
                    "{}/{}".format(counters.replySent, counters.replyRecv),
                    "{}/{}".format(counters.updateSent, counters.updateRecv),
                    "{}/{}".format(counters.totalSent, counters.totalRecv),
                ]
            )
        seg = printing.render_horizontal_table(rows, column_labels, tablefmt="plain")
        output.append([cap, seg])
    print(printing.render_vertical_table(output))
