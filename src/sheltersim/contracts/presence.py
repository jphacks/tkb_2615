"""Shared typed presence/relocation receipts; domain Policies own actual transitions."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from sheltersim.contracts.extensions import ExtensionModel, Identifier

PRESENCE_NAMESPACE="population_presence_v1"


class PresenceRecord(ExtensionModel):
    agent_id: Identifier
    household_id: Identifier | None = None
    status: Literal["admitted","approaching","awaiting_admission","temporarily_absent","departed","transferred"]
    since_minute: int = Field(strict=True,ge=0)
    transition_zone_id: Identifier | None = None
    return_zone_id: Identifier | None = None
    expected_return_minute: int | None = Field(default=None,strict=True,ge=0)
    # A reference to state retained by the domain owner, not a duplicate AgentState.
    archive_path: tuple[Identifier,...] | None = Field(default=None,min_length=1,max_length=5)
    source_event_ids: tuple[Identifier,...] = ()

    @model_validator(mode="after")
    def coherent_return(self) -> PresenceRecord:
        if self.status == "temporarily_absent" and self.expected_return_minute is not None and self.expected_return_minute<self.since_minute:
            raise ValueError("expected return precedes departure")
        if self.status=="admitted" and self.transition_zone_id is None:
            raise ValueError("admitted presence requires a zone")
        if self.status=="temporarily_absent" and not self.archive_path:
            raise ValueError("temporary absence requires a retained state reference")
        return self


class PresenceTransition(ExtensionModel):
    schema_version: Literal[2] = 2
    transition_id: Identifier
    event_id: Identifier
    agent_id: Identifier
    minute: int = Field(strict=True,ge=0)
    action: Literal["arrived","approaching","approach_completed","waiting","admitted","departed","temporary_exit","returned","transferred","reunited","relocated"]
    from_zone_id: Identifier | None = None
    to_zone_id: Identifier | None = None
    from_household_id: Identifier | None = None
    to_household_id: Identifier | None = None
    causation_id: Identifier | None = None
    source_ids: tuple[Identifier,...] = ()
    state_snapshot_hash: str | None = Field(default=None,pattern=r"^[a-f0-9]{64}$")
    time_semantics: Literal["recorded_event_minute"] = "recorded_event_minute"


class PresenceState(ExtensionModel):
    schema_version: Literal[1] = 1
    people: dict[Identifier,PresenceRecord]
    history: tuple[PresenceTransition,...] = Field(default=(),max_length=20000)


def validate_presence_state(world: Any, raw: dict[str,Any]) -> None:
    state=PresenceState.model_validate(raw)
    for key,person in state.people.items():
        if key!=person.agent_id or person.since_minute>world.minute_index:
            raise ValueError("presence identity/time mismatch")
        admitted=key in world.agents
        if (person.status=="admitted")!=admitted:
            raise ValueError("presence admission disagrees with WorldState")
        if person.transition_zone_id is not None and person.transition_zone_id not in world.zones:
            raise ValueError("unknown transition zone")
        if person.return_zone_id is not None and person.return_zone_id not in world.zones:
            raise ValueError("unknown return zone")
        if person.archive_path:
            value=world.model_state
            for part in person.archive_path:
                if not isinstance(value,dict) or part not in value:
                    raise ValueError("presence retained-state reference is missing")
                value=value[part]
    if not set(world.agents)<=set(state.people):
        raise ValueError("presence omits an admitted person")
    ids=set()
    previous=-1
    for receipt in state.history:
        if receipt.transition_id in ids or receipt.agent_id not in state.people or receipt.minute<previous or receipt.minute>world.minute_index:
            raise ValueError("presence receipt identity/order/time mismatch")
        if any(zone is not None and zone not in world.zones for zone in (receipt.from_zone_id,receipt.to_zone_id)):
            raise ValueError("presence receipt references an unknown zone")
        ids.add(receipt.transition_id);previous=receipt.minute


def register_presence_contract() -> None:
    """Explicit installed bootstrap hook; never enabled by importing a JSON scenario."""
    from sheltersim.contracts.runtime import register_state_contract, state_catalog
    from sheltersim.contracts.workspace import StateDescriptor
    descriptor=StateDescriptor(namespace=PRESENCE_NAMESPACE,implementation_id="population-presence-ledger-v1",
        implementation_version="1",schema_version=1,entity_collection="people",
        fields={"status":{"unit":"presence_state","public":True},"household_id":{"unit":"identifier","public":True},
                "transition_zone_id":{"unit":"identifier","public":True,"semantics":"last_transition_location_not_current_position"},"expected_return_minute":{"unit":"minute","public":True}})
    present=next((row for row in state_catalog() if row["namespace"]==PRESENCE_NAMESPACE),None)
    if present is not None:
        if present!=descriptor.model_dump(mode="json"):raise ValueError("presence contract already registered differently")
        return
    def initial(world: Any) -> dict[str,Any]:
        return PresenceState(people={key:PresenceRecord(agent_id=key,household_id=agent.profile.group_id,
            status="admitted",since_minute=world.minute_index,transition_zone_id=agent.zone_id)
            for key,agent in world.agents.items()}).model_dump(mode="json")
    def projection(state: Any, entity: str) -> dict[str,Any]:
        row=state["people"].get(entity,{})
        return {key:row.get(key) for key in descriptor.fields}
    register_state_contract(descriptor,validator=validate_presence_state,initializer=initial,projector=projection)
