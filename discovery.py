import logging
import asyncio
from providers import TravelpayoutsProvider
from db import get_all_manual_legs, get_all_transit_hubs

logger = logging.getLogger("discovery")

MAX_DISCOVERY_FIRST_HUBS = 35

class RouteDiscoveryService:
    def __init__(self, provider: TravelpayoutsProvider):
        self.provider = provider
        self.last_diagnostics = {}

    async def discover_candidate_edges(self, origin: str, destination_country: str, 
                                       destination_iatas: list[str], months: list[str], 
                                       max_transfers: int) -> set[tuple[str, str]]:
        edges, _ = await self.discover_candidate_edges_with_diagnostics(
            origin=origin,
            destination_country=destination_country,
            destination_iatas=destination_iatas,
            months=months,
            max_transfers=max_transfers,
        )
        return edges

    async def discover_candidate_edges_with_diagnostics(
        self,
        origin: str,
        destination_country: str,
        destination_iatas: list[str],
        months: list[str],
        max_transfers: int
    ) -> tuple[set[tuple[str, str]], dict]:
        """
        Performs bidirectional search to discover candidate flight segments (edges).
        Builds a target-specific topology:
        - direct validation edges: origin -> target airports;
        - one-transfer edges: origin -> hub -> target, only for hubs proven on both sides;
        - two-transfer bridge edges: origin -> first hub -> target-side hub -> target.
        """
        logger.info(f"Starting route discovery: {origin} -> {destination_country} (max transfers: {max_transfers})")
        destination_set = set(destination_iatas)
        target_side_edges = set()
        
        # 1. Forward Discovery: Direct destinations from Origin
        forward_hubs = set()
        forward_edges = set()
        for month in months:
            routes = await self.provider.get_outbound_directions(origin, month)
            for r in routes:
                if r["transfers"] == 0:
                    dest = r["destination"]
                    forward_hubs.add(dest)
                    forward_edges.add((origin, dest))
                    
        logger.info(f"Forward discovery: found {len(forward_hubs)} direct destinations from {origin}")
        
        # 2. Backward Discovery: Direct origins to target destination airports
        backward_hubs = set()
        backward_edges = set()
        for dest in destination_iatas:
            for month in months:
                routes = await self.provider.get_inbound_directions(dest, month)
                for r in routes:
                    if r["transfers"] == 0:
                        route_origin = r["origin"]
                        route_dest = r["destination"]
                        backward_hubs.add(route_origin)
                        backward_edges.add((route_origin, route_dest))
                        target_side_edges.add((route_origin, route_dest))
                        
        logger.info(f"Backward discovery: found {len(backward_hubs)} direct origins to target destinations")

        transit_catalog = {hub["iata"] for hub in get_all_transit_hubs()}

        def clean_transit_hubs(hubs: set[str]) -> set[str]:
            return {
                hub for hub in hubs
                if hub and hub != origin and hub not in destination_set
            }

        forward_transit_hubs = clean_transit_hubs(forward_hubs)
        backward_transit_hubs = clean_transit_hubs(backward_hubs)
        one_transfer_hubs = forward_transit_hubs.intersection(backward_transit_hubs)

        def first_hub_rank(hub: str) -> tuple[int, str]:
            score = 0
            if hub in one_transfer_hubs:
                score += 100
            if hub in transit_catalog:
                score += 20
            return (-score, hub)

        first_hubs = sorted(forward_transit_hubs, key=first_hub_rank)[:MAX_DISCOVERY_FIRST_HUBS]
        first_hubs_set = set(first_hubs)
        logger.info(f"First-layer hubs selected for bridge discovery: {len(first_hubs)}")
        
        candidate_edges = set()
        direct_validation_edges = set()
        one_transfer_origin_edges = set()
        one_transfer_target_edges = set()
        bridge_edges = set()
        bridge_target_edges = set()
        
        # Direct flights: Origin -> Destination
        for dest in destination_iatas:
            direct_validation_edges.add((origin, dest))
        candidate_edges.update(direct_validation_edges)

        if max_transfers >= 1:
            one_transfer_origin_edges = {
                edge for edge in forward_edges
                if edge[1] in one_transfer_hubs or edge[1] in destination_set
            }
            one_transfer_target_edges = {
                edge for edge in backward_edges
                if edge[0] in one_transfer_hubs
            }
            candidate_edges.update(one_transfer_origin_edges)
            candidate_edges.update(one_transfer_target_edges)

        # 4. Explore target-specific connections between first-layer hubs and target-side hubs.
        # This is intentionally not "any discovered hub -> any discovered hub": that broad
        # expansion made different countries produce suspiciously similar candidate graphs.
        if max_transfers >= 2:
            logger.info("Exploring first-layer hubs for target-side bridge connections...")
            for hub in first_hubs:
                for month in months:
                    routes = await self.provider.get_outbound_directions(hub, month)
                    for r in routes:
                        if r["transfers"] == 0:
                            connected_city = r["destination"]
                            if connected_city == hub:
                                continue
                            if connected_city in backward_transit_hubs:
                                bridge_edges.add((hub, connected_city))
                            elif connected_city in destination_set:
                                bridge_target_edges.add((hub, connected_city))

            candidate_edges.update({(origin, hub) for hub in first_hubs_set})
            candidate_edges.update(target_side_edges)
            candidate_edges.update(bridge_edges)
            candidate_edges.update(bridge_target_edges)
                                
        # Include manual ground legs
        manual_legs = get_all_manual_legs()
        manual_candidate_count = 0
        for leg in manual_legs:
            leg_origin = leg["origin"]
            leg_dest = leg["destination"]
            
            can_reach_leg = (
                leg_origin == origin
                or leg_origin in first_hubs_set
                or leg_origin in one_transfer_hubs
                or leg_origin in backward_transit_hubs
            )
            can_reach_from_leg = (
                leg_dest in destination_set
                or leg_dest in backward_transit_hubs
                or leg_dest in first_hubs_set
            )
            
            if can_reach_leg and can_reach_from_leg:
                logger.info(f"Adding manual leg candidate: {leg_origin} -> {leg_dest}")
                manual_candidate_count += 1
                # Manual legs are loaded directly by the solver. Do not invent flight
                # edges around them here; only provider-proven edges should be priced.

        diagnostics = {
            "algorithm": "layered_mitm_v2",
            "months": months,
            "destination_iatas": destination_iatas,
            "forward_hubs_count": len(forward_transit_hubs),
            "backward_hubs_count": len(backward_transit_hubs),
            "one_transfer_hubs_count": len(one_transfer_hubs),
            "selected_first_hubs_count": len(first_hubs),
            "selected_first_hubs": first_hubs[:20],
            "edge_categories": {
                "direct_validation": len(direct_validation_edges),
                "one_transfer_origin": len(one_transfer_origin_edges),
                "one_transfer_target": len(one_transfer_target_edges),
                "target_side": len(target_side_edges),
                "bridge": len(bridge_edges),
                "bridge_to_target": len(bridge_target_edges),
                "manual_candidates": manual_candidate_count,
            },
            "candidate_edges_count": len(candidate_edges),
        }
        self.last_diagnostics = diagnostics
        logger.info(f"Total unique candidate edges identified: {len(candidate_edges)}")
        return candidate_edges, diagnostics
