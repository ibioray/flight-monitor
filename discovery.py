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
        for month in months:
            routes = await self.provider.get_outbound_directions(origin, month)
            for r in routes:
                if r["transfers"] == 0:
                    forward_hubs.add(r["destination"])
                    
        logger.info(f"Forward discovery: found {len(forward_hubs)} direct destinations from {origin}")
        
        # 2. Backward Discovery: Direct origins to target destination airports
        backward_hubs = set()
        for dest in destination_iatas:
            for month in months:
                routes = await self.provider.get_inbound_directions(dest, month)
                for r in routes:
                    if r["transfers"] == 0:
                        backward_hubs.add(r["origin"])
                        
        logger.info(f"Backward discovery: found {len(backward_hubs)} direct origins to target destinations")
        
        # 3. Create the pool of all transit hubs (union of forward and backward direct hubs)
        all_hubs = forward_hubs.union(backward_hubs)
        if origin in all_hubs:
            all_hubs.remove(origin)
        for dest in destination_iatas:
            if dest in all_hubs:
                all_hubs.remove(dest)
                
        # Limit to top 35 hubs to avoid query spikes, but keeping all major candidates
        hubs_list = list(all_hubs)[:35]
        logger.info(f"Total hubs to explore for topology: {len(hubs_list)}")
        
        candidate_edges = set()
        
        # Direct flights: Origin -> Destination
        for dest in destination_iatas:
            candidate_edges.add((origin, dest))
            
        # Direct flights: Origin -> Hub
        for hub in forward_hubs:
            candidate_edges.add((origin, hub))
            
        # Direct flights: Hub -> Destination
        for hub in backward_hubs:
            for dest in destination_iatas:
                candidate_edges.add((hub, dest))
                
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
                if leg_origin != origin and leg_origin not in forward_hubs:
                    candidate_edges.add((origin, leg_origin))
                if leg_dest not in destination_iatas and leg_dest not in backward_hubs:
                    for dest in destination_iatas:
                        candidate_edges.add((leg_dest, dest))
                        
        logger.info(f"Total unique candidate edges identified: {len(candidate_edges)}")
        return candidate_edges
