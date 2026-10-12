# SPDX-License-Identifier: AGPL-3.0-only
"""Fixed SRC timeline offsets on the owned DSP path, not propagated port delays."""
from __future__ import annotations

import json
import subprocess
import time

from audio.tool_env import c_locale_env


def graph_src_latency(graph: list[dict], *, include_ingress: bool = True,
                      ingress_nodes: tuple[str, ...] | None = None) -> float:
    nodes = {item['id']: item.get('info') or {} for item in graph
             if item.get('type') == 'PipeWire:Interface:Node'}
    names = {index: (info.get('props') or {}).get('node.name') for index, info in nodes.items()}
    engines = [index for index, name in names.items() if name == 'fxroute_dsp']
    if len(engines) != 1:
        raise RuntimeError('SRC latency graph has no unique DSP engine')
    engine = engines[0]
    incoming, outgoing = {}, {}
    for item in graph:
        info = item.get('info') or {}
        if item.get('type') != 'PipeWire:Interface:Link' or info.get('state') not in {'active', 'paused'}:
            continue
        source, target = info['output-node-id'], info['input-node-id']
        incoming.setdefault(target, set()).add(source)
        outgoing.setdefault(source, set()).add(target)

    def own_delay(index):
        info = nodes.get(index)
        if info is None:
            raise RuntimeError('SRC latency graph contains a missing node')
        params = {}
        for entry in (info.get('params') or {}).get('Props', []):
            values = entry.get('params') or {}
            params.update(values if isinstance(values, dict) else zip(values[::2], values[1::2]))
        bound = (info.get('props') or {}).get('audio.adapt.converter') == 'fxroute.src.convert'
        if not bound and 'fxroute.src.mode' not in params:
            return 0
        if params.get('fxroute.src.error') or params.get('fxroute.src.mode') in {None, 'unconfigured'}:
            raise RuntimeError('SRC latency adapter is unavailable')
        if params.get('fxroute.src.nominal-bypass') is True:
            return 0
        value = params.get('fxroute.src.latency-ns')
        if type(value) is not int or not 0 <= value <= 20000000000:
            raise RuntimeError('SRC latency adapter evidence is unavailable')
        return value

    def upstream(index, visited):
        if index in visited:
            raise RuntimeError('SRC latency graph contains a cycle')
        delay = own_delay(index)
        sources = incoming.get(index, ())
        return {delay + value for source in sources for value in upstream(source, visited | {index})} if sources else {delay}

    def downstream(index, visited):
        if index in visited:
            raise RuntimeError('SRC latency graph contains a cycle')
        info = nodes.get(index) or {}
        props = info.get('props') or {}
        if props.get('media.class') == 'Stream/Input/Audio':
            return set()
        delay = own_delay(index)
        if props.get('media.class') == 'Audio/Sink' and names.get(index) != 'fxroute_dsp_sink':
            return {delay}
        return {delay + value for target in outgoing.get(index, ())
                for value in downstream(target, visited | {index})}

    source_delays = set()
    if include_ingress:
        for source in incoming.get(engine, ()):
            if ingress_nodes is None or names.get(source) in ingress_nodes:
                source_delays.update(upstream(source, {engine}))
    output_delays = {value for target in outgoing.get(engine, ()) for value in downstream(target, {engine})}
    if include_ingress and not source_delays:
        raise RuntimeError('Owned SRC ingress path is unavailable')
    if not output_delays:
        raise RuntimeError('Owned SRC output path is unavailable')
    if len(source_delays) > 1 or len(output_delays) > 1:
        raise RuntimeError('Linked audio paths have different SRC latency')
    return (next(iter(source_delays), 0) + next(iter(output_delays), 0)) / 1000000


class SrcLatencyReader:
    """Bound graph sampling cost without caching obsolete path ownership."""
    def __init__(self):
        self._cache = None

    def read(self, *, include_ingress: bool = True, ingress_nodes: tuple[str, ...] | None = None,
             owner: object = None) -> float:
        now = time.monotonic()
        scope = owner, include_ingress, ingress_nodes
        cache = self._cache
        if cache is None or scope != cache[2] or now - cache[1] >= 0.5:
            try:
                result = subprocess.run(['pw-dump'], capture_output=True, text=True, timeout=5,
                                        check=True, env=c_locale_env())
                graph = json.loads(result.stdout)
                if not isinstance(graph, list):
                    raise ValueError('Invalid PipeWire graph')
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                raise RuntimeError('SRC latency graph is unavailable') from exc
            self._cache = graph, now, scope
        else:
            graph = cache[0]
        return graph_src_latency(graph, include_ingress=include_ingress, ingress_nodes=ingress_nodes)
