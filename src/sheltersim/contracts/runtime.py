"""Installed versioned model-state contracts, with no executable scenario expressions."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, Mapping, Protocol

from pydantic import Field, model_validator

from sheltersim.contracts.extensions import ExtensionModel, Finite, Identifier
from sheltersim.contracts.workspace import StateDescriptor
from sheltersim.core.state_mirror import rust_validated

if TYPE_CHECKING:
    from sheltersim.core.rng import KeyedRandom
    from sheltersim.policies.behavior import WorldView


class StateContribution(ExtensionModel):
    entity_id: Identifier
    entity_kind: Literal["agent", "staff"] = "agent"
    field: Identifier
    before: Finite
    after: Finite
    unit: str = Field(min_length=1, max_length=80)
    source_ids: tuple[Identifier, ...] = ()
    cause_ids: tuple[Identifier, ...] = ()


class ModelStateProposal(ExtensionModel):
    schema_version: Literal[1] = 1
    namespace: Identifier
    model_id: Identifier
    minute: int = Field(strict=True, ge=0)
    proposed_state: dict[str, Any]
    contributions: tuple[StateContribution, ...] = ()

    @model_validator(mode="after")
    def state_is_finite_json(self) -> ModelStateProposal:
        import json

        json.dumps(self.proposed_state, allow_nan=False)
        return self


class StatefulPolicy(Protocol):
    def propose(
        self, view: WorldView, minute: int, runtime_state: Mapping[str, Any], rng: KeyedRandom
    ) -> ModelStateProposal: ...


def apply_model_proposal(world: Any, proposal: ModelStateProposal) -> None:
    """Controller-only atomic namespace replacement; a Policy cannot edit other state."""
    descriptor = declared_states(world.model_configs).get(proposal.namespace)
    if descriptor is None:
        raise ValueError("proposal namespace is not declared")
    from sheltersim.contracts.policies import selected_models

    model_id = (
        selected_models(world.model_configs).get(descriptor.policy_domain)
        if descriptor.policy_domain
        else descriptor.implementation_id
    )
    if model_id != proposal.model_id or proposal.minute != world.minute_index:
        raise ValueError("proposal model/namespace/time does not match this world")
    for contribution in proposal.contributions:
        field = descriptor.fields.get(contribution.field)
        if (
            field is None
            or field.get("unit") != contribution.unit
            or contribution.entity_kind != descriptor.entity_kind
            or contribution.entity_id
            not in (world.agents if contribution.entity_kind == "agent" else world.staff)
            or not set(contribution.source_ids) <= set(descriptor.source_ids)
        ):
            raise ValueError("proposal contribution references an undeclared field/entity/source")
    # A contribution is a checkable numerical receipt, not free-form trace prose.
    collection = descriptor.entity_collection or (
        "agents" if descriptor.entity_kind == "agent" else "staff"
    )
    for contribution in proposal.contributions:
        metadata = descriptor.fields[contribution.field]
        path = metadata.get("state_path", [contribution.field])
        if (
            not isinstance(path, (list, tuple))
            or not path
            or any(not isinstance(part, str) for part in path)
        ):
            raise ValueError("invalid installed contribution state path")
        before = (
            world.model_state[proposal.namespace]
            .get(collection, {})
            .get(contribution.entity_id, {})
        )
        after = proposal.proposed_state.get(collection, {}).get(contribution.entity_id, {})
        for part in path:
            before = before.get(part) if isinstance(before, dict) else None
            after = after.get(part) if isinstance(after, dict) else None
        if (
            type(before) not in (int, float)
            or type(after) not in (int, float)
            or before != contribution.before
            or after != contribution.after
        ):
            raise ValueError("contribution values disagree with proposed state")
    candidate = type(world).from_dict(world.to_dict())
    candidate.model_state[proposal.namespace] = deepcopy(proposal.proposed_state)
    candidate.validate()
    world.model_state[proposal.namespace] = deepcopy(candidate.model_state[proposal.namespace])


# Installed code metadata and validators, never mutable simulation state.
_CONTRACTS: dict[
    str, tuple[StateDescriptor, Callable[..., Any], Callable[..., Any], Callable[..., Any] | None]
] = {}


def register_state_contract(
    descriptor: StateDescriptor,
    *,
    validator: Callable[..., Any],
    initializer: Callable[..., Any],
    projector: Callable[..., Any] | None = None,
) -> None:
    if descriptor.namespace in _CONTRACTS:
        raise ValueError("state contract already installed")
    if not callable(validator) or not callable(initializer):
        raise ValueError("state contracts require installed validation/initialization functions")
    if projector is not None and not callable(projector):
        raise ValueError("model-state projector must be installed code")
    _CONTRACTS[descriptor.namespace] = (descriptor, validator, initializer, projector)


def declared_states(configs: dict[str, Any]) -> dict[str, StateDescriptor]:
    from sheltersim.contracts.bootstrap import ensure_builtin_contracts

    ensure_builtin_contracts()
    declaration = configs.get("state_contracts")
    if declaration is None:
        return {}
    if (
        set(declaration) != {"schema_version", "namespaces"}
        or type(declaration["schema_version"]) is not int
        or declaration["schema_version"] != 1
    ):
        raise ValueError("unsupported state_contracts declaration")
    if not isinstance(declaration["namespaces"], dict):
        raise ValueError("state_contracts namespaces must be an object")
    result = {}
    for namespace, requested in declaration["namespaces"].items():
        if namespace not in _CONTRACTS:
            raise ValueError("requested model-state contract is not installed")
        descriptor = _CONTRACTS[namespace][0]
        signature = {
            "implementation_id": descriptor.implementation_id,
            "implementation_version": descriptor.implementation_version,
            "schema_version": descriptor.schema_version,
        }
        if (
            not isinstance(requested, dict)
            or type(requested.get("schema_version")) is not int
            or requested != signature
        ):
            raise ValueError("model-state implementation/schema mismatch; migration required")
        if descriptor.policy_domain:
            from sheltersim.contracts.policies import model_catalog, selected_models

            selected = selected_models(configs).get(descriptor.policy_domain)
            if (
                selected is None
                or model_catalog()[selected]["state_schema_version"] != descriptor.schema_version
            ):
                raise ValueError("selected policy cannot use this installed state schema")
        result[namespace] = descriptor
    return result


def state_signatures(configs: dict[str, Any]) -> dict[str, str]:
    return {
        f"state:{namespace}": (
            f"{descriptor.implementation_id}@{descriptor.implementation_version}:state{descriptor.schema_version}"
        )
        for namespace, descriptor in declared_states(configs).items()
    }


def initialize_declared_states(world: Any) -> None:
    for namespace in declared_states(world.model_configs):
        if namespace not in world.model_state:
            world.model_state[namespace] = _CONTRACTS[namespace][2](world)
    validate_declared_states(world)


def validate_declared_states(world: Any, *, tick_memo: Any = None) -> None:
    """Check the declared namespaces; see core/tick_validation.py for tick_memo."""
    if tick_memo is None or not tick_memo.configs_unchanged:
        descriptors = declared_states(world.model_configs)
        if tick_memo is not None:
            tick_memo.declared_states = descriptors
    else:
        descriptors = tick_memo.declared_states
    for namespace, descriptor in descriptors.items():
        state = world.model_state.get(namespace)
        if (
            not isinstance(state, dict)
            or type(state.get("schema_version")) is not int
            or state.get("schema_version") != descriptor.schema_version
        ):
            raise ValueError("missing or incompatible required model-state namespace")
        if (
            tick_memo is not None
            and tick_memo.presence_unchanged
            and namespace == "population_presence_v1"
        ):
            continue
        try:
            if not rust_validated(world, namespace):
                _CONTRACTS[namespace][1](world, state)
        except (TypeError, KeyError, ValueError) as exc:
            raise ValueError("declared model-state validation failed") from exc


def state_catalog() -> list[dict[str, Any]]:
    return [value[0].model_dump(mode="json") for _, value in sorted(_CONTRACTS.items())]


def entity_projections(
    world: Any, *, entity_ids: set[str] | None = None
) -> dict[tuple[str, str], dict[str, Any]]:
    """Observe public model fields through immutable JSON snapshots, without World/RNG."""
    import json

    def frozen(value: Any) -> Any:
        if isinstance(value, Mapping):
            return MappingProxyType({key: frozen(item) for key, item in value.items()})
        if isinstance(value, (list, tuple)):
            return tuple(frozen(item) for item in value)
        return value

    def plain(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {key: plain(item) for key, item in value.items()}
        if isinstance(value, tuple):
            return [plain(item) for item in value]
        return value

    result: dict[tuple[str, str], dict[str, Any]] = {}
    for namespace, descriptor in declared_states(world.model_configs).items():
        projector = _CONTRACTS[namespace][3]
        if projector is None:
            continue
        state = frozen(world.model_state[namespace])
        public = {
            key for key, metadata in descriptor.fields.items() if metadata.get("public") is True
        }
        entities = world.agents if descriptor.entity_kind == "agent" else world.staff
        for entity_id in sorted(
            entities if entity_ids is None else set(entities).intersection(entity_ids)
        ):
            projected = plain(projector(state, entity_id))
            if not isinstance(projected, dict) or not set(projected) <= public:
                raise ValueError("model-state projection contains undeclared public fields")
            if len(json.dumps(projected, allow_nan=False).encode()) > 8192:
                raise ValueError("model-state entity projection exceeds 8 KiB")
            if projected:
                result.setdefault((descriptor.entity_kind, entity_id), {})[namespace] = projected
    return result
