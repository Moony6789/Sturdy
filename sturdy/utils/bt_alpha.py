import asyncio

import bittensor as bt
import numpy as np
from async_lru import alru_cache


# Create tasks for fetching metagraph data
@alru_cache(maxsize=512)
async def fetch_metagraph(sub: bt.AsyncSubtensor, block: int, netuid: int) -> tuple[int, bt.MetagraphInfo]:
    try:
        metagraph = await sub.get_metagraph_info(netuid=netuid, block=block)
        bt.logging.trace(f"Fetched data for block {block}")
    except Exception as e:
        bt.logging.error(f"Error fetching data for block {block}")
        bt.logging.exception(e)
        return block, None
    else:
        return block, metagraph


# Create tasks for fetching dynamicinfo for a subnet
@alru_cache(maxsize=512)
async def fetch_dynamic_info(sub: bt.AsyncSubtensor, block: int, netuid: int) -> bt.DynamicInfo:
    try:
        dynamic_info = await sub.subnet(netuid=netuid, block=block)
        bt.logging.trace(f"Fetched data for block {block}")
    except Exception as e:
        bt.logging.error(f"Error fetching data for block {block}")
        bt.logging.exception(e)
        return None
    else:
        return dynamic_info


# Create tasks for fetching dividends of nominator from a validator and timestamps
@alru_cache(maxsize=512)
async def fetch_nominator_dividends(sub: bt.AsyncSubtensor, block: int, hotkey: str, netuid: int) -> tuple[int, int]:
    if netuid is None:
        return block, None, None
    try:
        uid = await sub.get_uid_for_hotkey_on_subnet(
            hotkey_ss58=hotkey,
            netuid=netuid,
            block=block,
        )
        if uid is None:
            return block, None
        take = await sub.get_delegate_take(hotkey_ss58=hotkey, block=block)
        _, metagraph = await fetch_metagraph(sub=sub, block=block, netuid=netuid)
        dividends = metagraph.alpha_dividends_per_hotkey[uid][1].tao * (1 - take)  # remove validator take
        bt.logging.trace(f"Fetched dividends for block {block}: {dividends}")
    except Exception as e:
        bt.logging.error(f"Error fetching dividends for block {block}: {e}")
        return block, None
    else:
        return block, dividends


@alru_cache(maxsize=512)
async def fetch_total_nominator_alpha_stake(sub: bt.AsyncSubtensor, block: int, hotkey: str, netuid: int) -> tuple[int, float]:
    try:
        uid = await sub.get_uid_for_hotkey_on_subnet(
            hotkey_ss58=hotkey,
            netuid=netuid,
            block=block,
        )
        if uid is None:
            return block, None
        raw_total_hotkey_alpha = await sub.query_subtensor(name="TotalHotkeyAlpha", params=[hotkey, netuid])
        total_hotkey_alpha = bt.Balance.from_rao(getattr(raw_total_hotkey_alpha, "value", 0), netuid=netuid)
        alpha_staked = total_hotkey_alpha.tao
    except Exception as e:
        bt.logging.error(f"Error fetching total nominator alpha stake for {hotkey} at block {block}: {e}")
        return block, 0
    else:
        return block, alpha_staked


@alru_cache(maxsize=512)
async def get_vali_avg_apy(
    subtensor: bt.AsyncSubtensor,
    netuid: int,
    hotkey: str,
    block: int,
    end_block: int | None,
    interval: int | None = None,
    delta_alpha_tao: float = 0.0,
) -> int:
    ending_block = end_block if end_block is not None else await subtensor.block
    if block >= ending_block:
        return 0

    dynamic_info = await subtensor.subnet(netuid=netuid)
    if interval is None:
        interval = dynamic_info.tempo
    last_epoch_block = dynamic_info.last_step
    lookback = ending_block - block
    starting_block = last_epoch_block - lookback

    blocks = list(range(starting_block, last_epoch_block, interval))

    # Fetch dividends concurrently
    dividends_tasks = [fetch_nominator_dividends(sub=subtensor, block=block, hotkey=hotkey, netuid=netuid) for block in blocks]
    dividends_results = await asyncio.gather(*dividends_tasks)

    alpha_stake_tasks = [
        fetch_total_nominator_alpha_stake(sub=subtensor, block=block, hotkey=hotkey, netuid=netuid) for block in blocks
    ]
    alpha_stake_results = dict(await asyncio.gather(*alpha_stake_tasks))

    nominator_earnings = {block: (divs) for block, divs in dividends_results if divs is not None}

    mean_apy_pct = 0
    try:
        nominator_apy_pct = np.array(
            # TODO: should "7280" be variable? - dependant on tempo (360 on all subnets)?
            # 7280 is approx. seconds per year /avg block time/360
            # TODO: should divs scale proportionally with increasing alpha delta?
            [
                ((1 + (divs / (alpha_stake_results[block] + delta_alpha_tao))) ** 7280) - 1
                if alpha_stake_results[block] > 0
                else 0
                for block, divs in nominator_earnings.items()
                if divs is not None
            ]
        )
        # debug log mean
        mean_apy_pct = np.nan_to_num(nominator_apy_pct.mean())
    except Exception as e:
        bt.logging.debug(f"Error calculating alpha apy, assuming it to be 0: {e}")
        return 0
    else:
        return mean_apy_pct
