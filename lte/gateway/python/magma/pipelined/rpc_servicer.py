"""
Copyright 2020 The Magma Authors.

This source code is licensed under the BSD-style license found in the
LICENSE file in the root directory of this source tree.

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""
import os
import logging
import queue
from concurrent.futures import Future
from itertools import chain
from typing import List, Tuple

import grpc
from lte.protos import pipelined_pb2_grpc
from lte.protos.pipelined_pb2 import (
    SetupFlowsResult,
    RequestOriginType,
    ActivateFlowsResult,
    DeactivateFlowsResult,
    FlowResponse,
    RuleModResult,
    SetupUEMacRequest,
    SetupPolicyRequest,
    SetupQuotaRequest,
    ActivateFlowsRequest,
    AllTableAssignments,
    TableAssignment)
from lte.protos.policydb_pb2 import PolicyRule
from lte.protos.mobilityd_pb2 import IPAddress
from lte.protos.subscriberdb_pb2 import AggregatedMaximumBitrate
from magma.pipelined.app.dpi import DPIController
from magma.pipelined.app.enforcement import EnforcementController
from magma.pipelined.app.enforcement_stats import EnforcementStatsController
from magma.pipelined.app.ue_mac import UEMacAddressController
from magma.pipelined.app.ipfix import IPFIXController
from magma.pipelined.app.check_quota import CheckQuotaController
from magma.pipelined.app.vlan_learn import VlanLearnController
from magma.pipelined.app.tunnel_learn import TunnelLearnController
from magma.pipelined.policy_converters import convert_ipv4_str_to_ip_proto, \
    convert_ipv6_bytes_to_ip_proto
from magma.pipelined.ipv6_prefix_store import get_ipv6_interface_id, get_ipv6_prefix
from magma.pipelined.metrics import (
    ENFORCEMENT_STATS_RULE_INSTALL_FAIL,
    ENFORCEMENT_RULE_INSTALL_FAIL,
)

grpc_msg_queue = queue.Queue()


class PipelinedRpcServicer(pipelined_pb2_grpc.PipelinedServicer):
    """
    gRPC based server for Pipelined.
    """

    def __init__(self, loop, gy_app, enforcer_app, enforcement_stats, dpi_app,
                 ue_mac_app, check_quota_app, ipfix_app, vlan_learn_app,
                 tunnel_learn_app, classifier_app, inout_app, ng_servicer_app,
                 service_config, service_manager):
        self._loop = loop
        self._gy_app = gy_app
        self._enforcer_app = enforcer_app
        self._enforcement_stats = enforcement_stats
        self._dpi_app = dpi_app
        self._ue_mac_app = ue_mac_app
        self._check_quota_app = check_quota_app
        self._ipfix_app = ipfix_app
        self._vlan_learn_app = vlan_learn_app
        self._tunnel_learn_app = tunnel_learn_app
        self._service_config = service_config
        self._classifier_app = classifier_app
        self._inout_app = inout_app
        self._ng_servicer_app = ng_servicer_app
        self._service_manager = service_manager

        self._print_grpc_payload = os.environ.get('MAGMA_PRINT_GRPC_PAYLOAD')
        if self._print_grpc_payload is None:
            self._print_grpc_payload = \
                service_config.get('magma_print_grpc_payload', False)

    def add_to_server(self, server):
        """
        Add the servicer to a gRPC server
        """
        pipelined_pb2_grpc.add_PipelinedServicer_to_server(self, server)

    # --------------------------
    # General setup rpc
    # --------------------------

    def SetupDefaultControllers(self, request, _) -> SetupFlowsResult:
        """
        Setup default controllers, used on pipelined restarts
        """
        self._log_grpc_payload(request)
        ret = self._inout_app.check_setup_request_epoch(request.epoch)
        if ret is not None:
            return SetupFlowsResult(result=ret)

        fut = Future()
        self._loop.call_soon_threadsafe(self._setup_default_controllers, fut)
        return fut.result()

    def _setup_default_controllers(self, fut: 'Future(SetupFlowsResult)'):
        res = self._inout_app.handle_restart(None)
        fut.set_result(res)

    # --------------------------
    # Enforcement App
    # --------------------------

    def SetupPolicyFlows(self, request, context) -> SetupFlowsResult:
        """
        Setup flows for all subscribers, used on pipelined restarts
        """
        self._log_grpc_payload(request)
        if not self._service_manager.is_app_enabled(
                EnforcementController.APP_NAME):
            context.set_code(grpc.StatusCode.UNAVAILABLE)
            context.set_details('Service not enabled!')
            return None

        for controller in [self._gy_app, self._enforcer_app,
                           self._enforcement_stats]:
            ret = controller.check_setup_request_epoch(request.epoch)
            if ret is not None:
                return SetupFlowsResult(result=ret)

        fut = Future()
        self._loop.call_soon_threadsafe(self._setup_flows, request, fut)
        return fut.result()

    def _setup_flows(self, request: SetupPolicyRequest,
                     fut: 'Future[List[SetupFlowsResult]]'
                     ) -> SetupFlowsResult:
        gx_reqs = [req for req in request.requests
                   if req.request_origin.type == RequestOriginType.GX]
        gy_reqs = [req for req in request.requests
                   if req.request_origin.type == RequestOriginType.GY]
        enforcement_res = self._enforcer_app.handle_restart(gx_reqs)
        # TODO check these results and aggregate
        self._gy_app.handle_restart(gy_reqs)
        self._enforcement_stats.handle_restart(gx_reqs)
        fut.set_result(enforcement_res)

    def ActivateFlows(self, request, context):
        """
        Activate flows for a subscriber based on the pre-defined rules
        """
        self._log_grpc_payload(request)
        if not self._service_manager.is_app_enabled(
                EnforcementController.APP_NAME):
            context.set_code(grpc.StatusCode.UNAVAILABLE)
            context.set_details('Service not enabled!')
            return None

        fut = Future()  # type: Future[ActivateFlowsResult]
        self._loop.call_soon_threadsafe(self._activate_flows, request, fut)
        return fut.result()

    def _update_ipv6_prefix_store(self, ipv6_addr: bytes):
        ipv6_str = ipv6_addr.decode('utf-8')
        interface = get_ipv6_interface_id(ipv6_str)
        prefix = get_ipv6_prefix(ipv6_str)
        self._service_manager.interface_to_prefix_mapper.save_prefix(
            interface, prefix)

    def _update_tunnel_map_store(self, uplink_tunnel: int,
                                 downlink_tunnel: int):
        self._service_manager.tunnel_id_mapper.save_tunnels(uplink_tunnel,
                                                            downlink_tunnel)

    def _update_version(self, request: ActivateFlowsRequest, ipv4: IPAddress):
        """
        Update version for a given subscriber and rule.
        """
        for rule_id in request.rule_ids:
            self._service_manager.session_rule_version_mapper.update_version(
                request.sid.id, ipv4, rule_id)
        for rule in request.dynamic_rules:
            self._service_manager.session_rule_version_mapper.update_version(
                request.sid.id, ipv4, rule.id)

    def _activate_flows(self, request: ActivateFlowsRequest,
                        fut: 'Future[ActivateFlowsResult]'
                        ) -> None:
        """
        Activate flows for ipv4 / ipv6 or both

        CWF won't have an ip_addr passed
        """
        ret = ActivateFlowsResult()
        if self._service_config['setup_type'] == 'CWF' or request.ip_addr:
            ipv4 = convert_ipv4_str_to_ip_proto(request.ip_addr)
            if request.request_origin.type == RequestOriginType.GX:
                ret_ipv4 = self._install_flows_gx(request, ipv4)
            else:
                ret_ipv4 = self._install_flows_gy(request, ipv4)
            ret.static_rule_results.extend(ret_ipv4.static_rule_results)
            ret.dynamic_rule_results.extend(ret_ipv4.dynamic_rule_results)
        if request.ipv6_addr:
            ipv6 = convert_ipv6_bytes_to_ip_proto(request.ipv6_addr)
            self._update_ipv6_prefix_store(request.ipv6_addr)
            if request.request_origin.type == RequestOriginType.GX:
                ret_ipv6 = self._install_flows_gx(request, ipv6)
            else:
                ret_ipv6 = self._install_flows_gy(request, ipv6)
            ret.static_rule_results.extend(ret_ipv6.static_rule_results)
            ret.dynamic_rule_results.extend(ret_ipv6.dynamic_rule_results)
        if request.uplink_tunnel and request.downlink_tunnel:
            self._update_tunnel_map_store(request.uplink_tunnel,
                                          request.downlink_tunnel)

        fut.set_result(ret)

    def _install_flows_gx(self, request: ActivateFlowsRequest,
                         ip_address: IPAddress
                         ) -> ActivateFlowsResult:
        """
        Ensure that the RuleModResult is only successful if the flows are
        successfully added in both the enforcer app and enforcement_stats.
        Install enforcement_stats flows first because even if the enforcement
        flow install fails after, no traffic will be directed to the
        enforcement_stats flows.
        """
        logging.debug('Activating GX flows for %s', request.sid.id)
        self._update_version(request, ip_address)
        # Install rules in enforcement stats
        enforcement_stats_res = self._activate_rules_in_enforcement_stats(
            request.sid.id, request.msisdn, request.uplink_tunnel, ip_address, request.apn_ambr, request.rule_ids,
            request.dynamic_rules)

        failed_static_rule_results, failed_dynamic_rule_results = \
            _retrieve_failed_results(enforcement_stats_res)
        # Do not install any rules that failed to install in enforcement_stats.
        static_rule_ids = \
            _filter_failed_static_rule_ids(request, failed_static_rule_results)
        dynamic_rules = \
            _filter_failed_dynamic_rules(request, failed_dynamic_rule_results)

        enforcement_res = self._activate_rules_in_enforcement(
            request.sid.id, request.msisdn, request.uplink_tunnel, ip_address, request.apn_ambr, static_rule_ids,
            dynamic_rules)

        # Include the failed rules from enforcement_stats in the response.
        enforcement_res.static_rule_results.extend(failed_static_rule_results)
        enforcement_res.dynamic_rule_results.extend(
            failed_dynamic_rule_results)
        return enforcement_res

    def _install_flows_gy(self, request: ActivateFlowsRequest,
                          ip_address: IPAddress
                          ) -> ActivateFlowsResult:
        """
        Ensure that the RuleModResult is only successful if the flows are
        successfully added in both the enforcer app and enforcement_stats.
        Install enforcement_stats flows first because even if the enforcement
        flow install fails after, no traffic will be directed to the
        enforcement_stats flows.
        """
        logging.debug('Activating GY flows for %s', request.sid.id)
        self._update_version(request, ip_address)
        # Install rules in enforcement stats
        enforcement_stats_res = self._activate_rules_in_enforcement_stats(
            request.sid.id, request.msisdn, request.uplink_tunnel, ip_address, request.apn_ambr,
            request.rule_ids, request.dynamic_rules)

        failed_static_rule_results, failed_dynamic_rule_results = \
            _retrieve_failed_results(enforcement_stats_res)
        # Do not install any rules that failed to install in enforcement_stats.
        static_rule_ids = \
            _filter_failed_static_rule_ids(request, failed_static_rule_results)
        dynamic_rules = \
            _filter_failed_dynamic_rules(request, failed_dynamic_rule_results)

        gy_res = self._activate_rules_in_gy(request.sid.id, request.msisdn, request.uplink_tunnel,
                                            ip_address, request.apn_ambr, static_rule_ids,
                                            dynamic_rules)

        # Include the failed rules from enforcement_stats in the response.
        gy_res.static_rule_results.extend(failed_static_rule_results)
        gy_res.dynamic_rule_results.extend(failed_dynamic_rule_results)
        return gy_res

    def _activate_rules_in_enforcement_stats(self, imsi: str,
                                             msisdn: bytes,
                                             uplink_tunnel: int,
                                             ip_addr: IPAddress,
                                             apn_ambr: AggregatedMaximumBitrate,
                                             static_rule_ids: List[str],
                                             dynamic_rules: List[PolicyRule]
                                             ) -> ActivateFlowsResult:
        if not self._service_manager.is_app_enabled(
                EnforcementStatsController.APP_NAME):
            return ActivateFlowsResult()

        enforcement_stats_res = self._enforcement_stats.activate_rules(
            imsi, msisdn, uplink_tunnel, ip_addr, apn_ambr, static_rule_ids, dynamic_rules)
        _report_enforcement_stats_failures(enforcement_stats_res, imsi)
        return enforcement_stats_res

    def _activate_rules_in_enforcement(self, imsi: str, msisdn: bytes,
                                       uplink_tunnel: int,
                                       ip_addr: IPAddress,
                                       apn_ambr: AggregatedMaximumBitrate,
                                       static_rule_ids: List[str],
                                       dynamic_rules: List[PolicyRule]
                                       ) -> ActivateFlowsResult:
        # TODO: this will crash pipelined if called with both static rules
        # and dynamic rules at the same time
        enforcement_res = self._enforcer_app.activate_rules(
            imsi, msisdn, uplink_tunnel, ip_addr, apn_ambr, static_rule_ids, dynamic_rules)
        # TODO ?? Should the enforcement failure be reported per imsi session
        _report_enforcement_failures(enforcement_res, imsi)
        return enforcement_res

    def _activate_rules_in_gy(self, imsi: str, msisdn: bytes,
                              uplink_tunnel: int,
                              ip_addr: IPAddress,
                              apn_ambr: AggregatedMaximumBitrate,
                              static_rule_ids: List[str],
                              dynamic_rules: List[PolicyRule]
                              ) -> ActivateFlowsResult:
        gy_res = self._gy_app.activate_rules(imsi, msisdn, uplink_tunnel,
                                             ip_addr, apn_ambr,
                                             static_rule_ids,
                                             dynamic_rules)
        # TODO: add metrics
        return gy_res

    def DeactivateFlows(self, request, context):
        """
        Deactivate flows for a subscriber
        """
        self._log_grpc_payload(request)
        if not self._service_manager.is_app_enabled(
                EnforcementController.APP_NAME):
            context.set_code(grpc.StatusCode.UNAVAILABLE)
            context.set_details('Service not enabled!')
            return None

        self._loop.call_soon_threadsafe(self._deactivate_flows, request)
        return DeactivateFlowsResult()

    def _deactivate_flows(self, request):
        """
        Deactivate flows for ipv4 / ipv6 or both

        CWF won't have an ip_addr passed
        """
        if self._service_config['setup_type'] == 'CWF' or request.ip_addr:
            ipv4 = convert_ipv4_str_to_ip_proto(request.ip_addr)
            if request.request_origin.type == RequestOriginType.GX:
                self._deactivate_flows_gx(request, ipv4)
            else:
                self._deactivate_flows_gy(request, ipv4)
        if request.ipv6_addr:
            ipv6 = convert_ipv6_bytes_to_ip_proto(request.ipv6_addr)
            self._update_ipv6_prefix_store(request.ipv6_addr)
            if request.request_origin.type == RequestOriginType.GX:
                self._deactivate_flows_gx(request, ipv6)
            else:
                self._deactivate_flows_gy(request, ipv6)

    def _deactivate_flows_gx(self, request, ip_address: IPAddress):
        logging.debug('Deactivating GX flows for %s', request.sid.id)
        if request.rule_ids:
            for rule_id in request.rule_ids:
                self._service_manager.session_rule_version_mapper \
                    .update_version(request.sid.id, ip_address,
                                    rule_id)
        else:
            # If no rule ids are given, all flows are deactivated
            self._service_manager.session_rule_version_mapper.update_version(
                request.sid.id, ip_address)
        if request.remove_default_drop_flows:
            self._enforcement_stats.deactivate_default_flow(request.sid.id,
                                                            ip_address)
        self._enforcer_app.deactivate_rules(request.sid.id, ip_address,
                                            request.rule_ids)

    def _deactivate_flows_gy(self, request, ip_address: IPAddress):
        logging.debug('Deactivating GY flows for %s', request.sid.id)
        # Only deactivate requested rules here to not affect GX
        if request.rule_ids:
            for rule_id in request.rule_ids:
                self._service_manager.session_rule_version_mapper \
                    .update_version(request.sid.id, ip_address, rule_id)
        self._gy_app.deactivate_rules(request.sid.id, ip_address,
                                      request.rule_ids)

    def GetPolicyUsage(self, request, context):
        """
        Get policy usage stats
        """
        self._log_grpc_payload(request)
        if not self._service_manager.is_app_enabled(
                EnforcementStatsController.APP_NAME):
            context.set_code(grpc.StatusCode.UNAVAILABLE)
            context.set_details('Service not enabled!')
            return None

        fut = Future()
        self._loop.call_soon_threadsafe(
            self._enforcement_stats.get_policy_usage, fut)
        return fut.result()

    # --------------------------
    # IPFIX App
    # --------------------------

    def UpdateIPFIXFlow(self, request, context):
        """
        Update IPFIX sampling record
        """
        self._log_grpc_payload(request)
        if self._service_manager.is_app_enabled(IPFIXController.APP_NAME):
            # Install trace flow
            self._loop.call_soon_threadsafe(
                self._ipfix_app.add_ue_sample_flow, request.sid.id,
                request.msisdn, request.ap_mac_addr, request.ap_name,
                request.pdp_start_time)

        resp = FlowResponse()
        return resp

    # --------------------------
    # DPI App
    # --------------------------

    def CreateFlow(self, request, context):
        """
        Add dpi flow
        """
        self._log_grpc_payload(request)
        if not self._service_manager.is_app_enabled(
                DPIController.APP_NAME):
            context.set_code(grpc.StatusCode.UNAVAILABLE)
            context.set_details('Service not enabled!')
            return None
        resp = FlowResponse()
        self._loop.call_soon_threadsafe(self._dpi_app.add_classify_flow,
                                        request.match, request.state,
                                        request.app_name, request.service_type)
        return resp

    def RemoveFlow(self, request, context):
        """
        Add dpi flow
        """
        self._log_grpc_payload(request)
        if not self._service_manager.is_app_enabled(
                DPIController.APP_NAME):
            context.set_code(grpc.StatusCode.UNAVAILABLE)
            context.set_details('Service not enabled!')
            return None
        resp = FlowResponse()
        self._loop.call_soon_threadsafe(self._dpi_app.remove_classify_flow,
                                        request.match)
        return resp

    def UpdateFlowStats(self, request, context):
        """
        Update stats for a flow
        """
        self._log_grpc_payload(request)
        if not self._service_manager.is_app_enabled(
                DPIController.APP_NAME):
            context.set_code(grpc.StatusCode.UNAVAILABLE)
            context.set_details('Service not enabled!')
            return None
        resp = FlowResponse()
        return resp

    # --------------------------
    # UE MAC App
    # --------------------------

    def SetupUEMacFlows(self, request, context) -> SetupFlowsResult:
        """
        Activate a list of attached UEs
        """
        self._log_grpc_payload(request)
        if not self._service_manager.is_app_enabled(
                UEMacAddressController.APP_NAME):
            context.set_code(grpc.StatusCode.UNAVAILABLE)
            context.set_details('Service not enabled!')
            return None

        ret = self._ue_mac_app.check_setup_request_epoch(request.epoch)
        if ret is not None:
            return SetupFlowsResult(result=ret)

        fut = Future()
        self._loop.call_soon_threadsafe(self._setup_ue_mac,
                                        request, fut)
        return fut.result()

    def _setup_ue_mac(self, request: SetupUEMacRequest,
                      fut: 'Future(SetupFlowsResult)'
                      ) -> SetupFlowsResult:
        res = self._ue_mac_app.handle_restart(request.requests)

        if self._service_manager.is_app_enabled(IPFIXController.APP_NAME):
            for req in request.requests:
                self._ipfix_app.add_ue_sample_flow(req.sid.id, req.msisdn,
                                                   req.ap_mac_addr,
                                                   req.ap_name,
                                                   req.pdp_start_time)

        fut.set_result(res)

    def AddUEMacFlow(self, request, context):
        """
        Associate UE MAC address to subscriber
        """
        self._log_grpc_payload(request)
        if not self._service_manager.is_app_enabled(
                UEMacAddressController.APP_NAME):
            context.set_code(grpc.StatusCode.UNAVAILABLE)
            context.set_details('Service not enabled!')
            return None

        # 12 hex characters + 5 colons
        if len(request.mac_addr) != 17:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details('Invalid UE MAC address provided')
            return None

        fut = Future()
        self._loop.call_soon_threadsafe(self._add_ue_mac_flow, request, fut)

        return fut.result()

    def _add_ue_mac_flow(self, request, fut: 'Future(FlowResponse)'):
        res = self._ue_mac_app.add_ue_mac_flow(request.sid.id, request.mac_addr)

        fut.set_result(res)

    def DeleteUEMacFlow(self, request, context):
        """
        Delete UE MAC address to subscriber association
        """
        self._log_grpc_payload(request)
        if not self._service_manager.is_app_enabled(
                UEMacAddressController.APP_NAME):
            context.set_code(grpc.StatusCode.UNAVAILABLE)
            context.set_details('Service not enabled!')
            return None

        # 12 hex characters + 5 colons
        if len(request.mac_addr) != 17:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details('Invalid UE MAC address provided')
            return None

        self._loop.call_soon_threadsafe(
            self._ue_mac_app.delete_ue_mac_flow,
            request.sid.id, request.mac_addr)

        if self._service_manager.is_app_enabled(CheckQuotaController.APP_NAME):
            self._loop.call_soon_threadsafe(
                self._check_quota_app.remove_subscriber_flow, request.sid.id)

        if self._service_manager.is_app_enabled(VlanLearnController.APP_NAME):
            self._loop.call_soon_threadsafe(
                self._vlan_learn_app.remove_subscriber_flow, request.sid.id)

        if self._service_manager.is_app_enabled(TunnelLearnController.APP_NAME):
            self._loop.call_soon_threadsafe(
                self._tunnel_learn_app.remove_subscriber_flow, request.mac_addr)

        if self._service_manager.is_app_enabled(IPFIXController.APP_NAME):
            # Delete trace flow
            self._loop.call_soon_threadsafe(
                self._ipfix_app.delete_ue_sample_flow, request.sid.id)

        resp = FlowResponse()
        return resp

    # --------------------------
    # Check Quota App
    # --------------------------

    def SetupQuotaFlows(self, request, context) -> SetupFlowsResult:
        """
        Activate a list of quota rules
        """
        self._log_grpc_payload(request)
        if not self._service_manager.is_app_enabled(
                CheckQuotaController.APP_NAME):
            context.set_code(grpc.StatusCode.UNAVAILABLE)
            context.set_details('Service not enabled!')
            return None

        ret = self._check_quota_app.check_setup_request_epoch(request.epoch)
        if ret is not None:
            return SetupFlowsResult(result=ret)

        fut = Future()
        self._loop.call_soon_threadsafe(self._setup_quota,
                                        request, fut)
        return fut.result()

    def _setup_quota(self, request: SetupQuotaRequest,
                     fut: 'Future(SetupFlowsResult)'
                     ) -> SetupFlowsResult:
        res = self._check_quota_app.handle_restart(request.requests)
        fut.set_result(res)

    def UpdateSubscriberQuotaState(self, request, context):
        """
        Updates the subcsciber quota state
        """
        self._log_grpc_payload(request)
        if not self._service_manager.is_app_enabled(
                CheckQuotaController.APP_NAME):
            context.set_code(grpc.StatusCode.UNAVAILABLE)
            context.set_details('Service not enabled!')
            return None

        resp = FlowResponse()
        self._loop.call_soon_threadsafe(
            self._check_quota_app.update_subscriber_quota_state, request.updates)
        return resp

    # --------------------------
    # Debugging
    # --------------------------

    def GetAllTableAssignments(self, request, context):
        """
        Get the flow table assignment for all apps ordered by main table number
        and name
        """
        self._log_grpc_payload(request)
        table_assignments = self._service_manager.get_all_table_assignments()
        return AllTableAssignments(table_assignments=[
            TableAssignment(app_name=app_name, main_table=tables.main_table,
                            scratch_tables=tables.scratch_tables) for
            app_name, tables in table_assignments.items()])

    # --------------------------
    # Internal
    # --------------------------

    def _log_grpc_payload(self, grpc_request):
        if not grpc_request:
            return
        indent = '  '
        dbl_indent = indent + indent
        indented_text = dbl_indent + \
            str(grpc_request).replace('\n', '\n' + dbl_indent)
        log_msg = 'Got RPC payload:\n{0}{1} {{\n{2}\n{0}}}'.format(indent,
            grpc_request.DESCRIPTOR.name, indented_text.rstrip())

        grpc_msg_queue.put(log_msg)
        if grpc_msg_queue.qsize() > 100:
            grpc_msg_queue.get()

        if not self._print_grpc_payload:
            return
        logging.info(log_msg)

def _retrieve_failed_results(activate_flow_result: ActivateFlowsResult
                             ) -> Tuple[List[RuleModResult],
                                        List[RuleModResult]]:
    failed_static_rule_results = \
        [result for result in activate_flow_result.static_rule_results
         if result.result == RuleModResult.FAILURE]
    failed_dynamic_rule_results = \
        [result for result in
         activate_flow_result.dynamic_rule_results if
         result.result == RuleModResult.FAILURE]
    return failed_static_rule_results, failed_dynamic_rule_results


def _filter_failed_static_rule_ids(request: ActivateFlowsRequest,
                                   failed_results: List[RuleModResult]
                                   ) -> List[str]:
    failed_static_rule_ids = [result.rule_id for result in failed_results]
    return [rule_id for rule_id in request.rule_ids if
            rule_id not in failed_static_rule_ids]


def _filter_failed_dynamic_rules(request: ActivateFlowsRequest,
                                 failed_results: List[RuleModResult]
                                 ) -> List[PolicyRule]:
    failed_dynamic_rule_ids = [result.rule_id for result in failed_results]
    return [rule for rule in request.dynamic_rules if
            rule.id not in failed_dynamic_rule_ids]


def _report_enforcement_failures(activate_flow_result: ActivateFlowsResult,
                                 imsi: str):
    rule_results = chain(activate_flow_result.static_rule_results,
                         activate_flow_result.dynamic_rule_results)
    for result in rule_results:
        if result.result == RuleModResult.SUCCESS:
            continue
        ENFORCEMENT_RULE_INSTALL_FAIL.labels(rule_id=result.rule_id,
                                             imsi=imsi).inc()


def _report_enforcement_stats_failures(
        activate_flow_result: ActivateFlowsResult,
        imsi: str):
    rule_results = chain(activate_flow_result.static_rule_results,
                         activate_flow_result.dynamic_rule_results)
    for result in rule_results:
        if result.result == RuleModResult.SUCCESS:
            continue
        ENFORCEMENT_STATS_RULE_INSTALL_FAIL.labels(rule_id=result.rule_id,
                                                   imsi=imsi).inc()
