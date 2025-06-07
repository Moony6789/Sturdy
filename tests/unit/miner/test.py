import math
import asyncio
from typing import Dict, List, cast, Annotated
from web3.constants import ADDRESS_ZERO
from web3 import AsyncWeb3, AsyncHTTPProvider
import bittensor as bt
from pydantic import BaseModel, Field
import json

from sturdy.utils.misc import (
    async_retry_with_backoff,
    generate_random_partition_np,
    getReserveFactor,
    rayMul,
    retry_with_backoff,
)

from sturdy.base.miner import BaseMinerNeuron
from sturdy.pools import (
    POOL_TYPES,
    BittensorAlphaTokenPool,
    PoolFactory,
    get_minimum_allocation,
)
from sturdy.protocol import AllocateAssets
from sturdy.providers import POOL_DATA_PROVIDER_TYPE

THRESHOLD = 1
INCREMENT_STEP = 3
# Fix: PoolModel type annotation and import
PoolModel = Annotated[BaseModel, Field(discriminator="pool_model_disc")]

class OptimizerContext:
    def __init__(self, pools, web3_provider, total_assets, num_allocs):
        self.pools = pools
        self.web3_provider = web3_provider
        self.total_assets = total_assets
        self.pool_data_providers = {}
        self.num_allocs = num_allocs

    async def initialize_pools(self, synapse: AllocateAssets):
        """Initialize and sync all pools."""
        for addr, pool in self.pools.items():
            print(addr, pool)
            self.pools[addr] = PoolFactory.create_pool(
                pool_type=pool.pool_type,
                web3_provider=pool.pool_data_provider_type,
                user_address=(
                    pool.user_address if hasattr(pool, "user_address") and pool.user_address != ADDRESS_ZERO else synapse.user_address
                ),
                contract_address=pool.contract_address,
            )
            # Sync pool
            if not getattr(self.pools[addr], "_initted", False):
                await self.pools[addr].pool_init(self.web3_provider)
            await self.pools[addr].sync(self.web3_provider)

    async def get_marginal_apy(self, pool: BaseModel, current_allocation: int, increment: int) -> float:
        """Fetch marginal APY for allocating an increment to a pool."""
        decimals = 6
        if hasattr(pool, '_underlying_asset_contract'):
            decimals = await async_retry_with_backoff(pool._underlying_asset_contract.functions.decimals().call)
        amount_scaled = (current_allocation + increment) * 10 ** decimals // 10 ** 18
        print("Supply Rate Request:", amount_scaled)
        apy_wei = await pool.supply_rate(amount_scaled)
        return apy_wei  # You may want to convert this to float

    async def optimize_allocations(self, synapse: AllocateAssets, increment: int = 100_000_000_000_000_000_000) -> Dict[str, int]:

        """Distribute total_assets across pools to maximize yield."""
        self.pool_data_providers = {
            "ETHEREUM_MAINNET": self.web3_provider
        }
        await self.initialize_pools(synapse)
        allocations = {addr: 0 for addr in self.pools}
        remaining_assets = int(self.total_assets * THRESHOLD)
        marginal_apys = {}
        best_pool_addr = ADDRESS_ZERO
        increment = self.total_assets // INCREMENT_STEP
        while remaining_assets >= increment:
            # print("remaining assets", remaining_assets)
            # print("increment", increment)
            apy_tasks = [
                self.get_marginal_apy(pool, allocations[addr], increment)
                for addr, pool in self.pools.items()
            ]
            # print("apy_tasks=============", apy_tasks)
            apys = await asyncio.gather(*apy_tasks)
            marginal_apys = dict(zip(self.pools.keys(), apys))
            best_pool_addr = max(marginal_apys, key=marginal_apys.get)
            allocations[best_pool_addr] += increment
            remaining_assets -= increment
            print("allocations values:", allocations)
            pool = self.pools[best_pool_addr]
            decimals = 6
            # print("underlying_asset_contract", pool._underlying_asset_contract, pool._user_deposits)
            if hasattr(pool, '_underlying_asset_contract'):
                decimals = await async_retry_with_backoff(
                    pool._underlying_asset_contract.functions.decimals().call
                )
            if hasattr(pool, '_user_deposits'):
                pool._user_deposits += increment * 10 ** decimals // 10 ** 18


        if remaining_assets > 0 and marginal_apys:
            best_pool_addr = max(marginal_apys, key=marginal_apys.get)
            allocations[best_pool_addr] += remaining_assets

        print("allocations values:", allocations)
        non_zero_allocs = sum(1 for amt in allocations.values() if amt > 0)
       
        if non_zero_allocs < self.num_allocs:
            min_alloc = increment // 100
            for addr in allocations:
                if allocations[addr] == 0:
                    allocations[addr] = min_alloc
                    non_zero_allocs += 1
                    if non_zero_allocs >= self.num_allocs:
                        break
            excess = sum(allocations.values()) - int(self.total_assets * THRESHOLD)
            if excess > 0:
                largest_pool = max(allocations, key=allocations.get)
                allocations[largest_pool] -= excess

        return allocations

async def main():
    web3_provider = AsyncWeb3(AsyncHTTPProvider("https://eth-mainnet.g.alchemy.com/v2/aCIvdfrXjuQveR1JsKd3ME7AxkmZc4SW"))
    request = {
        "num_allocs": 3,
        "request_type": "ORGANIC",
        "user_address": "0x73E4C11B670Ef9C025A030A20b72CB9150E54523",
        "pool_data_provider_type": "ETHEREUM_MAINNET",
        "assets_and_pools": {
            "total_assets": 1120877955333353905234925,
            "pools": {
                "0x6311fF24fb15310eD3d2180D3d0507A21a8e5227": {
                    "pool_model_disc": "EVM_CHAIN_BASED",
                    "pool_type": "STURDY_SILO",
                    "contract_address": "0x6311fF24fb15310eD3d2180D3d0507A21a8e5227",
                    "pool_data_provider_type": "ETHEREUM_MAINNET"
                },
                "0x200723063111f9f8f1d44c0F30afAdf0C0b1a04b": {
                    "pool_model_disc": "EVM_CHAIN_BASED",
                    "pool_type": "STURDY_SILO",
                    "contract_address": "0x200723063111f9f8f1d44c0F30afAdf0C0b1a04b",
                    "pool_data_provider_type": "ETHEREUM_MAINNET"
                },
                "0x26fe402A57D52c8a323bb6e09f06489C8216aC88": {
                    "pool_model_disc": "EVM_CHAIN_BASED",
                    "pool_type": "STURDY_SILO",
                    "contract_address": "0x26fe402A57D52c8a323bb6e09f06489C8216aC88",
                    "pool_data_provider_type": "ETHEREUM_MAINNET"
                },
                "0x8dDE9A50a91cc0a5DaBdc5d3931c1AF60408c84D": {
                    "pool_model_disc": "EVM_CHAIN_BASED",
                    "pool_type": "STURDY_SILO",
                    "contract_address": "0x8dDE9A50a91cc0a5DaBdc5d3931c1AF60408c84D",
                    "pool_data_provider_type": "ETHEREUM_MAINNET"
                }
            }
        }
    }

    synapse = AllocateAssets(
        num_allocs=request["num_allocs"],
        request_type=request["request_type"],
        user_address=request["user_address"],
        pool_data_provider_type=request["pool_data_provider_type"],
        assets_and_pools=request["assets_and_pools"]
    )

    # Create pools dict for context
    num_allocs = request["num_allocs"]
    pools = synapse.assets_and_pools["pools"]
    total_assets = synapse.assets_and_pools["total_assets"]

    optimizer = OptimizerContext(pools, web3_provider, total_assets, num_allocs)
    allocations = await optimizer.optimize_allocations(synapse)

    result = {"allocations": allocations}
    print(json.dumps(result, indent=4))

if __name__ == "__main__":
    asyncio.run(main())