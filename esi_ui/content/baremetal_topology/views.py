import concurrent.futures

from django.views import generic

from esi.lib import nodes
from esi_ui.api import esi_api
from openstack_dashboard.dashboards.project.network_topology import views as topology_view


class IndexView(generic.TemplateView):
    template_name = 'project/baremetal_topology/index.html'


class ESIJSONView(topology_view.JSONView):
    def _get_servers(self, request):
        try:
            node_networks = nodes.network_list(esi_api.esiclient(request.user.token.id))
        except Exception:
            node_networks = []

        data = [
            {
                'id': node_network['node'].id,
                'name': node_network['node'].name,
                'connections': [
                    {
                        'mac_address': info['baremetal_port'].address,
                        'port_name': info['network_ports'][0].name if len(info['network_ports']) else '',
                    }
                    for info in node_network['network_info']
                ],
            }
            for node_network in node_networks
        ]
        return data

    def _get_networks(self, request):
        # Get neutron data
        # if we didn't specify tenant_id, all networks shown as admin user.
        # so it is need to specify the networks. However there is no need to
        # specify tenant_id for subnet. The subnet which belongs to the public
        # network is needed to draw subnet information on public network.
        try:
            # NOTE(amotoki):
            # To support auto allocated network in the network topology view,
            # we need to handle the auto allocated network which haven't been
            # created yet. The current network topology logic cannot not handle
            # fake network ID properly, so we temporarily exclude
            # pre-auto-allocated-network from the network topology view.
            # It would be nice if someone is interested in supporting it.
            neutron_networks = api.neutron.network_list_for_tenant(
                request,
                request.user.tenant_id,
                include_pre_auto_allocate=False)
        except Exception:
            neutron_networks = []
        networks = []
        for network in neutron_networks:
            allow_delete_subnet = policy.check(
                (("network", "delete_subnet"),),
                request,
                target={'network:tenant_id': getattr(network,
                                                     'tenant_id', None)}
            )
            obj = {'name': network.name_or_id,
                   'id': network.id,
                   'subnets': [{'id': subnet.id,
                                'cidr': subnet.cidr}
                               for subnet in network.subnets],
                   'status': self.trans.network[network.status],
                   'allow_delete_subnet': allow_delete_subnet,
                   'original_status': network.status,
                   'router:external': network['is_router_external']}
            self.add_resource_url('horizon:project:networks:subnets:detail',
                                  obj['subnets'])
            networks.append(obj)

        # Add public networks to the networks list
        if self.is_router_enabled:
            try:
                neutron_public_networks = api.neutron.network_list(
                    request,
                    **{'router:external': True})
            except Exception:
                neutron_public_networks = []
            my_network_ids = [net['id'] for net in networks]
            for publicnet in neutron_public_networks:
                if publicnet.id in my_network_ids:
                    continue
                try:
                    subnets = [{'id': subnet.id,
                                'cidr': subnet.cidr}
                               for subnet in publicnet.subnets]
                    self.add_resource_url(
                        'horizon:project:networks:subnets:detail', subnets)
                except Exception:
                    subnets = []
                networks.append({
                    'name': publicnet.name_or_id,
                    'id': publicnet.id,
                    'subnets': subnets,
                    'status': self.trans.network[publicnet.status],
                    'original_status': publicnet.status,
                    'router:external': publicnet['is_router_external']})

        self.add_resource_url('horizon:project:networks:detail',
                              networks)

        return sorted(networks,
                      key=lambda x: x.get('router:external'),
                      reverse=True)

    def _get_ports(self, request, networks):
        token = request.user.token.id
        try:
            with concurrent.futures.ThreadPoolExecutor() as executor:
                f1 = executor.submit(esi_api.esiclient(token).network.ports)
                f2 = executor.submit(esi_api.esiclient(token).baremetal.ports, details=True)
                f3 = executor.submit(esi_api.esiclient(token).network.ips)
                network_ports = list(f1.result())
                port_name_dict = {port.id: port.name for port in network_ports}
                
                node_id_dict = {}
                for port in f2.result():
                    port_id = port.internal_info.get('tenant_vif_port_id')
                    if port_id in node_id_dict:
                        node_id_dict[port_id].append(port.node_id)
                    elif port_id:
                        node_id_dict[port_id] = [port.node_id]

                floating_ips_dict = {}
                for fip in f3.result():
                    if fip.port_id in floating_ips_dict:
                        floating_ips_dict[fip.port_id].append(fip.floating_ip_address)
                    else:
                        floating_ips_dict[fip.port_id] = [fip.floating_ip_address]
        except Exception:
            network_ports = []

        for port in network_ports:
            if getattr(port, 'trunk_details') and port.trunk_details.get('sub_ports', []):
                for subport in port.trunk_details['sub_ports']:
                    subport['name'] = port_name_dict[subport['port_id']]

        tenant_network_ids = [network['id'] for network in networks]
        ports = [
            {
                'id': port.id,
                'name': port.name,
                'network_id': port.network_id,
                'server_ids': node_id_dict.get(port.id, []),
                'mac_address': port.mac_address,
                'sub_ports': port.trunk_details.get('sub_ports', []) if getattr(port, 'trunk_details') is not None else [],
                'device_id': port.device_id,
                'fixed_ips': port.fixed_ips,
                'floating_ips': floating_ips_dict.get(port.id, []),
                'device_owner': port.device_owner,
                'status': self.trans.port[port.status],
                'original_status': port.status,
            }
            for port in network_ports
            if (port.device_owner != 'network:router_ha_interface' and
                port.network_id in tenant_network_ids)
        ]

        # trunk ports at the end because the javascript needs to process the others first
        ports.sort(key=lambda port: len(port['sub_ports']) != 0)

        return ports
