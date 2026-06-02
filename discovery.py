import logging
import asyncio
from providers import TravelpayoutsProvider
from db import get_all_manual_legs

logger = logging.getLogger("discovery")

class RouteDiscoveryService:
    def __init__(self, provider: TravelpayoutsProvider):
        self.provider = provider

    async def discover_candidate_edges(self, origin: str, destination_country: str, 
                                       destination_iatas: list[str], months: list[str], 
                                       max_transfers: int) -> set[tuple[str, str]]:
        """
        Performs bidirectional search to discover candidate flight segments (edges).
        Builds a unified topology by querying outbound direct directions from each discovered hub.
        """
        logger.info(f"Starting route discovery: {origin} -> {destination_country} (max transfers: {max_transfers})")
        
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
                        
        logger.info(f"Backward discovery: found {len(backward_hubs)} direct origins to target destinations")
        
        # 3. Create the pool of all transit hubs (union of forward and backward direct hubs)
        all_hubs = forward_hubs.union(backward_hubs)
        if origin in all_hubs:
            all_hubs.remove(origin)
        for dest in destination_iatas:
            if dest in all_hubs:
                all_hubs.remove(dest)
                
        # Rank hubs deterministically by connection density (Codex H)
        hub_popularity = {}
        for hub in all_hubs:
            score = 0
            if hub in forward_hubs:
                score += 10
            if hub in backward_hubs:
                score += 10
            hub_popularity[hub] = score
            
        # Sort by popularity score (descending), then alphabetically by hub code (ascending)
        sorted_hubs = sorted(all_hubs, key=lambda h: (-hub_popularity[h], h))
        hubs_list = sorted_hubs[:35]
        logger.info(f"Total hubs to explore for topology (sorted deterministically): {len(hubs_list)}")
        
        candidate_edges = set()
        
        # Direct flights: Origin -> Destination
        for dest in destination_iatas:
            candidate_edges.add((origin, dest))
            
        # Direct flights: Origin -> Hub
        candidate_edges.update(forward_edges)
            
        # Direct flights: Hub -> specific destination airport proven by inbound discovery.
        # Do not expand every backward hub to every target airport; that creates many empty API requests.
        candidate_edges.update(backward_edges)
                
        # 4. Explore connections between hubs
        # We query outbound directions from each hub in our pool
        if max_transfers >= 2:
            logger.info("Exploring outbound topologies from transit hubs to find cross-connections...")
            for hub in hubs_list:
                for month in months:
                    routes = await self.provider.get_outbound_directions(hub, month)
                    for r in routes:
                        if r["transfers"] == 0:
                            connected_city = r["destination"]
                            # If it connects to another hub in our pool, add it as a candidate edge
                            if connected_city in all_hubs and connected_city != hub:
                                candidate_edges.add((hub, connected_city))
                                
        # Include manual ground legs
        manual_legs = get_all_manual_legs()
        for leg in manual_legs:
            leg_origin = leg["origin"]
            leg_dest = leg["destination"]
            
            can_reach_leg = (leg_origin == origin) or (leg_origin in forward_hubs) or (leg_origin in all_hubs)
            can_reach_from_leg = (leg_dest in destination_iatas) or (leg_dest in backward_hubs) or (leg_dest in all_hubs)
            
            if can_reach_leg and can_reach_from_leg:
                logger.info(f"Adding manual leg candidate: {leg_origin} -> {leg_dest}")
                # Manual legs are loaded directly by the solver. Do not invent flight
                # edges around them here; only provider-proven edges should be priced.
                        
        logger.info(f"Total unique candidate edges identified: {len(candidate_edges)}")
        return candidate_edges
