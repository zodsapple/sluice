"""Pydantic models for a pipeline spec: the only thing a human (or the healing agent) edits."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class SheetSel(_Strict):
    name: str | None = None          # exact name (case-insensitive)
    pattern: str | None = None       # regex on sheet name
    strategy: Literal["best_header", "largest"] = "best_header"


class LocateSpec(_Strict):
    sheet: SheetSel = Field(default_factory=SheetSel)
    header: int | Literal["auto"] = "auto"   # 0-based row index, like ctd's header_row
    data_start: int | None = None            # 0-based first data row when it is above/below header+1 (e.g. group row first)
    header_rows: list[int] | None = None     # several 0-based rows = multi-level header, flattened as "top|sub"
    synthetic_header: list[str] | None = None   # the sheet has no header row: these names, data from data_start
    max_header_scan: int = 30
    stop_at_blank_row: bool = False
    skip_rows_matching: list[str] = Field(default_factory=list)  # regex tested on each row's first non-empty cell
    encoding: str | None = None      # csv only
    delimiter: str | None = None     # csv only


class MapEntry(_Strict):
    to: str
    required: bool = False
    aliases: list[str] = Field(default_factory=list)
    fuzzy: bool = True


class ShapeOp(BaseModel):
    """`op` selects a registered operation; every other key is that op's parameter."""
    model_config = ConfigDict(extra="allow")
    op: str

    def params(self) -> dict[str, Any]:
        return {k: v for k, v in self.model_dump().items() if k != "op"}


class OnDuplicate(_Strict):
    """No silent dedup: exact duplicates are kept unless the user says `drop`."""
    exact: Literal["drop", "keep"] = "keep"
    conflict: Literal["error", "first", "last", "sum", "latest", "earliest"] = "error"
    order_by: str | None = None          # column that decides `latest` / `earliest`


class KeysSpec(_Strict):
    cols: list[str] = Field(default_factory=list)
    pattern: str | None = None
    grain: list[str] = Field(default_factory=list)   # extra cols that, with `cols`, define uniqueness
    nullable: list[str] = Field(default_factory=list)  # key parts allowed to be empty (still part of uniqueness)
    on_invalid: Literal["drop", "error"] = "drop"
    on_duplicate: OnDuplicate = Field(default_factory=OnDuplicate)


class ColExpr(_Strict):
    col: str | None = None
    const: Any = None
    agg: str = "first"
    where: dict[str, Any] | None = None
    default: Any = None
    type: Literal["date", "number", "int", "text"] | None = None
    required: bool = False


class TemplateSpec(_Strict):
    path: str | None = None
    sheet: str | None = None
    header_row: int = 1                      # 1-based Excel row holding the headers
    columns: dict[str, ColExpr | str]
    group_by: list[str] = Field(default_factory=list)
    sort_by: list[str] = Field(default_factory=list)
    filename: str = "{stem}_out.xlsx"

    @field_validator("columns")
    @classmethod
    def _coerce(cls, v: dict[str, Any]) -> dict[str, ColExpr]:
        return {k: (ColExpr(col=e) if isinstance(e, str) else e) for k, e in v.items()}


class Agg(_Strict):
    sum: list[str] | None = None
    count_unique: str | None = None
    count_rows: bool = False
    duplicates: bool = False                         # rows that exactly repeat an earlier row
    split: list[str] = Field(default_factory=list)   # explode tokens before counting
    pattern: str | None = None                       # only tokens matching count
    where: dict[str, Any] | None = None              # scalar / list / {lt,le,gt,ge,eq,ne,isnull,notnull,startswith,regex}
    stage: Literal["mapped", "shaped", "keyed"] = "mapped"   # which frame a *source* aggregate reads

    @model_validator(mode="after")
    def _one(self) -> "Agg":
        n = sum(1 for x in (self.sum, self.count_unique, self.count_rows or None, self.duplicates or None)
                if x is not None)
        if n != 1:
            raise ValueError("Agg needs exactly one of sum / count_unique / count_rows / duplicates")
        return self


class Expected(_Strict):
    value: float | None = None                              # declared control value
    cell: dict[str, str] | None = None                      # {sheet: ..., ref: "F2"} read from the input workbook
    filename: str | None = None                             # regex with one group, applied to the file stem


class ControlSpec(_Strict):
    name: str
    source: Agg | None = None         # omit for a declared-only control: output must equal `expected`
    output: Agg
    expected: Expected | None = None
    dropped: Agg | None = None       # aggregate over removed rows when shaping changes the source columns
    tol: float = Field(default=0.01, ge=0)
    on_fail: Literal["halt", "warn"] = "halt"
    allow_dropped: bool = True       # source - dropped == output (conservation), sums only
    max_dropped_ratio: float | None = Field(default=None, ge=0, le=1)
    hint: str | None = None


class RuleSpec(_Strict):
    name: str | None = None
    type: Literal["not_null", "unique", "regex", "range", "in_set"]
    cols: list[str] = Field(default_factory=list)
    pattern: str | None = None
    min: float | None = None
    max: float | None = None
    values: list[Any] = Field(default_factory=list)
    on_fail: Literal["halt", "warn"] = "warn"


class SourceSpec(_Strict):
    """An additional input table (other sheet, or other file) prepared like the primary one and used by shape ops."""
    name: str
    file: str | None = None           # path; a runtime `extra_inputs` entry overrides it; none = same workbook
    locate: LocateSpec = Field(default_factory=LocateSpec)
    map: dict[str, MapEntry | str]
    shape: list[ShapeOp] = Field(default_factory=list)

    @field_validator("map")
    @classmethod
    def _coerce_map(cls, v: dict[str, Any]) -> dict[str, MapEntry]:
        return {k: (MapEntry(to=e) if isinstance(e, str) else e) for k, e in v.items()}


class HealingSpec(_Strict):
    enabled: bool = False
    mode: Literal["propose-only", "auto"] = "propose-only"
    max_attempts: int = 2
    redact_columns: list[str] = Field(default_factory=list)


class MatchSpec(_Strict):
    filename: list[str] = Field(default_factory=list)


class PipelineSpec(_Strict):
    name: str
    workflow: str = "generic"
    description: str = ""
    match: MatchSpec = Field(default_factory=MatchSpec)
    locate: LocateSpec = Field(default_factory=LocateSpec)
    map: dict[str, MapEntry | str]
    map_cutoff: float = 0.85
    const: dict[str, Any] = Field(default_factory=dict)
    measures: list[str] = Field(default_factory=list)  # internal numeric columns: dropped-value accounting, `sum` dedupe
    sources: list[SourceSpec] = Field(default_factory=list)
    shape: list[ShapeOp] = Field(default_factory=list)
    keys: KeysSpec = Field(default_factory=KeysSpec)
    template: TemplateSpec
    controls: list[ControlSpec] = Field(default_factory=list)
    rules: list[RuleSpec] = Field(default_factory=list)
    healing: HealingSpec = Field(default_factory=HealingSpec)

    @field_validator("map")
    @classmethod
    def _coerce_map(cls, v: dict[str, Any]) -> dict[str, MapEntry]:
        return {k: (MapEntry(to=e) if isinstance(e, str) else e) for k, e in v.items()}
