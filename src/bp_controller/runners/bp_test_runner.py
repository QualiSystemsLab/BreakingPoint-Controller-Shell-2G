import csv
import json
import re
import time

import io
from xml.etree import ElementTree

from bp_controller.flows.bp_load_configuration_file_flow import BPLoadConfigurationFileFlow
from bp_controller.flows.bp_load_pcap_file_flow import BPLoadPcapFileFlow
from bp_controller.flows.bp_port_reservation_flow import BPPortReservationFlow
from bp_controller.flows.bp_results_flow import BPResultsFlow
from bp_controller.flows.bp_statistics_flow import BPStatisticsFlow
from bp_controller.flows.bp_test_execution_flow import BPTestExecutionFlow
from bp_controller.flows.bp_test_network_flow import BPTestNetworkFlow
from bp_controller.helpers.bp_reservation_details import BPReservationDetails
from bp_controller.helpers.quali_rest_api_helper import QualiAPIHelper
from bp_controller.reservation_info import ReservationInfo
from cloudshell.tg.breaking_point.runners.bp_runner import BPRunner
from cloudshell.tg.breaking_point.runners.exceptions import BPRunnerException


class BPTestRunner(BPRunner):
    def __init__(self, context, logger, api, reservation_info):
        """
        Test runner, hold current configuration fo specific test
        :param context:
        :param logger:
        :param api:
        :param reservation_info:
        :type reservation_info: ReservationInfo
        """
        super(BPTestRunner, self).__init__(context, logger, api)
        self.reservation_info = reservation_info
        self._test_id = None
        self._test_name = None
        self._network_name = None
        self._interfaces = None
        self._group_id = None

        self.__reservation_flow = None
        self.__test_execution_flow = None
        self.__test_statistics_flow = None
        self.__test_results_flow = None
        self.__test_network_flow = None
        self.__reservation_details = None

    @property
    def _reservation_flow(self):
        """
        :return:
        :rtype: BPPortReservationFlow
        """
        if not self.__reservation_flow:
            self.__reservation_flow = BPPortReservationFlow(self.session_manager, self.logger)
        return self.__reservation_flow

    @property
    def _test_execution_flow(self):
        """
        :return:
        :rtype: BPTestExecutionFlow
        """
        if not self.__test_execution_flow:
            self.__test_execution_flow = BPTestExecutionFlow(self.session_manager, self.logger)
        return self.__test_execution_flow

    @property
    def _test_statistics_flow(self):
        """
        :return:
        :rtype: BPStatisticsFlow
        """
        if not self.__test_statistics_flow:
            self.__test_statistics_flow = BPStatisticsFlow(self.session_manager, self.logger)
        return self.__test_statistics_flow

    @property
    def _test_results_flow(self):
        """
        :return:
        :rtype: BPStatisticsFlow
        """
        if not self.__test_results_flow:
            self.__test_results_flow = BPResultsFlow(self.session_manager, self.logger)
        return self.__test_results_flow

    @property
    def _test_network_flow(self):
        """
        :return:
        :rtype: BPTestNetworkFlow
        """
        if not self.__test_network_flow:
            self.__test_network_flow = BPTestNetworkFlow(self.session_manager, self.logger)
        return self.__test_network_flow

    @property
    def _reservation_details(self):
        """
        :return:
        :rtype: BPReservationDetails
        """
        if not self.__reservation_details:
            self.__reservation_details = BPReservationDetails(self.context, self.logger, self.api)
        else:
            self.__reservation_details.api = self.api
            self.__reservation_details.context = self.context
            self.__reservation_details.logger = self.logger
        return self.__reservation_details

    def load_configuration(self, file_path):
        self._load_test_file(file_path)
        self._reserve_ports()

    def load_pcap(self, file_path):
        response_file_name = BPLoadPcapFileFlow(self.session_manager, self.logger).load_pcap(file_path)
        self._logger.info("Response received: " + str(response_file_name))
        file_name = file_path.split("\\")[-1].split(".")[0]
        if not re.search(response_file_name, file_name, re.IGNORECASE):
            raise BPRunnerException(self.__class__.__name__, 'Unable to load pcap file')

    def _load_test_file(self, test_file_path):
        self._test_name = BPLoadConfigurationFileFlow(self.session_manager,
                                                      self.logger).load_configuration(
            test_file_path)
        test_model = ElementTree.parse(test_file_path).getroot().find('testmodel')
        self._network_name = test_model.get('network')
        self._interfaces = []
        for interface in test_model.findall('interface'):
            self._interfaces.append(int(interface.get('number')))

    def _reserve_ports(self):
        # associating ports
        bp_test_interfaces = self._test_network_flow.get_interfaces(self._network_name) if self._network_name else {}
        cs_reserved_ports = self._reservation_details.get_chassis_ports()
        reservation_order = []
        self.logger.debug('CS reserved ports {}'.format(cs_reserved_ports))
        self.logger.debug('BP test interfaces {}'.format(bp_test_interfaces))
        for int_number in sorted(self._interfaces):
            bp_interface = bp_test_interfaces.get(int_number, None)
            if bp_interface and bp_interface in cs_reserved_ports:
                self.logger.debug('Associating interface {}'.format(bp_interface))
                reservation_order.append(cs_reserved_ports[bp_interface])
            else:
                raise BPRunnerException(self.__class__.__name__,
                                        'Cannot find Port with Logical name {} in the reservation'.format(bp_interface))

        # reserving ports in certain order
        self._group_id = self.reservation_info.reserve(self.context.reservation.reservation_id, reservation_order)
        self._logger.info("group id is {0}".format(self._group_id))
        self._logger.info("group ids are {0}".format(self.reservation_info._groups))
        self._reservation_flow.reserve_ports(self._group_id, reservation_order)

    def start_traffic(self, blocking):
        if not self._test_name:
            raise BPRunnerException(self.__class__.__name__, 'Load configuration first')
        try:
            self._test_id = self._test_execution_flow.start_traffic(self._test_name, self._group_id)
            if blocking.lower() == 'true':
                self._test_execution_flow.block_while_test_running(self._test_id)
                ports = self.reservation_info.unreserve(self.context.reservation.reservation_id)
                self._reservation_flow.unreserve_ports(ports)
        except Exception:
            ports = self.reservation_info.unreserve(self.context.reservation.reservation_id)
            self._reservation_flow.unreserve_ports(ports)
            raise

    def stop_traffic(self):
        if not self._test_id:
            raise BPRunnerException(self.__class__.__name__, 'Test id is not defined, run the test first')
        self._test_execution_flow.stop_traffic(self._test_id)
        ports = self.reservation_info.unreserve(self.context.reservation.reservation_id)
        self._reservation_flow.unreserve_ports(ports)

    def get_statistics(self, view_name, output_format):
        if not self._test_id:
            raise BPRunnerException(self.__class__.__name__, 'Test id is not defined, run the test first')
        result = self._test_statistics_flow.get_rt_statistics(self._test_id, view_name)
        if output_format.lower() == 'json':
            statistics = json.dumps(result, indent=4, sort_keys=True, ensure_ascii=False)
            # print statistics
            # self.api.WriteMessageToReservationOutput(self.context.reservation.reservation_id, statistics)
        elif output_format.lower() == 'csv':
            output = io.BytesIO()
            w = csv.DictWriter(output, result.keys())
            w.writeheader()
            w.writerow(result)
            statistics = output.getvalue().strip('\r\n')

            # self.api.WriteMessageToReservationOutput(self.context.reservation.reservation_id,
            #                                          output.getvalue().strip('\r\n'))
        else:
            raise BPRunnerException(self.__class__.__name__, 'Incorrect file format, supported csv or json only')
        return statistics

    def get_results(self):
        if not self._test_id:
            raise BPRunnerException(self.__class__.__name__, 'Test id is not defined, run the test first')
        pdf_result = self._test_results_flow.get_results(self._test_id)
        domain = "Global"
        if hasattr(self.context, 'reservation') and self.context.reservation:
            domain = self.context.reservation.domain

        if hasattr(self.context, 'remote_reservation') and self.context.remote_reservation:
            domain = self.context.remote_reservation.domain

        quali_api_helper = QualiAPIHelper(self.logger, self.context.connectivity.server_address, domain)
        quali_api_helper.login(self.context.connectivity.admin_auth_token)
        env_name = re.sub("\s+", "_", self.context.reservation.environment_name)
        test_id = re.sub("\s+", "_", self._test_id)
        file_name = "{0}_{1}.pdf".format(env_name, test_id)
        save_file_name = "D:\\tests\\reports\\{0}.pdf".format(self.context.reservation.reservation_id)
        quali_api_helper.upload_file(self.context.reservation.reservation_id, file_name=file_name,
                                     file_stream=pdf_result)
        with open(save_file_name, 'w') as result_file:
            result_file.write(pdf_result)

        return "Please check attachments for results"
