import json
import random
import traceback
from enum import Enum
from itertools import product
from typing import (AbstractSet, Any, Callable, Dict, List, Mapping, Optional,
                    Tuple, Union)

import numpy as np
import scipy.stats as stats
from otree.api import BasePlayer, Currency
from otree.currency import _CurrencyEncoder
from pydantic import BaseModel, Field, StrBytes, typing, validator
from pydantic.main import Extra
from pydantic.types import conint

IntStr = Union[int, str]
AbstractSetIntStr = AbstractSet[IntStr]
DictIntStrAny = Dict[IntStr, Any]
DictStrAny = Dict[str, Any]
MappingIntStrAny = Mapping[IntStr, Any]


class PydanticModel(BaseModel):
    def tuple(self: BaseModel) -> Tuple[Any]:
        """Return a tuple of the pydantic model's attribute values."""
        return tuple(self.dict().values())

    @classmethod
    def from_args(cls, *args, **kwargs) -> "PydanticModel":
        arg_fields = [field_name for field_name in cls.__fields__ if field_name not in kwargs]
        kwargs.update(dict(zip(arg_fields, args)))
        return cls(**kwargs)

    def __repr_args__(self) -> Any:
        return self.dict().items()

    def dict(
        self,
        *,
        include: Union["AbstractSetIntStr", "MappingIntStrAny"] = None,
        exclude: Union["AbstractSetIntStr", "MappingIntStrAny"] = None,
        by_alias: bool = False,
        skip_defaults: bool = None,
        exclude_unset: bool = False,
        exclude_defaults: bool = False,
        exclude_none: bool = False,
    ) -> "DictStrAny":
        return super().dict(
            include=include or set([c for c in self.__fields__]),
            exclude=exclude,
            by_alias=by_alias,
            skip_defaults=skip_defaults,
            exclude_unset=exclude_unset,
            exclude_defaults=exclude_defaults,
            exclude_none=exclude_none,
        )

    def json(
        self,
        *,
        include: Union["AbstractSetIntStr", "MappingIntStrAny"] = None,
        exclude: Union["AbstractSetIntStr", "MappingIntStrAny"] = None,
        by_alias: bool = False,
        skip_defaults: bool = None,
        exclude_unset: bool = False,
        exclude_defaults: bool = False,
        exclude_none: bool = False,
        encoder: Optional[Callable[[Any], Any]] = None,
        **dumps_kwargs: Any,
    ) -> str:
        return super().json(
            include=include or set([c for c in self.__fields__]),
            exclude=exclude,
            by_alias=by_alias,
            skip_defaults=skip_defaults,
            exclude_unset=exclude_unset,
            exclude_defaults=exclude_defaults,
            exclude_none=exclude_none,
            encoder=encoder,
            **dumps_kwargs,
        )


DISTRIBUTIONS = [
    dict(natural_mean=100, natural_sigma=50),
    dict(natural_mean=100, natural_sigma=100),
    dict(natural_mean=500, natural_sigma=150),
    dict(natural_mean=500, natural_sigma=150),  # 4th gets different costs
    dict(natural_mean=100, natural_sigma=50),
    dict(natural_mean=100, natural_sigma=100),
]


class UnitCosts(PydanticModel):
    rcpu: Currency = Currency(25)  # retail cost / unit
    wcpu: Currency = Currency(14)  # wholesale cost / unit
    scpu: Currency = Currency(6)  # salvage cost / unit

    class Config:
        json_encoders = dict(Currency=_CurrencyEncoder)
        arbitrary_types_allowed = True

    @classmethod
    def from_treatment(cls, treatment: "Treatment") -> "UnitCosts":
        if treatment.idx == 3:
            return UnitCosts(rcpu=Currency(24), wcpu=Currency(5.5), scpu=Currency(5))
        return UnitCosts()


class DisributionParameters(PydanticModel):
    mu: float
    sigma: float

    @classmethod
    def from_treatment(cls, treatment: "Treatment") -> "UnitCosts":
        natural_mean, natural_sigma = DISTRIBUTIONS[treatment.idx].values()

        # method of moments: https://en.wikipedia.org/wiki/Log-normal_distribution
        mu = np.log(natural_mean ** 2 / np.sqrt(natural_sigma ** 2 + natural_mean ** 2))
        sigma = np.sqrt(np.log(natural_sigma ** 2 / (natural_mean ** 2) + 1))
        return DisributionParameters(mu=mu, sigma=sigma)


# import numpy as np
# import scipy.stats as stats
# natural_mean, natural_sigma = 100, 50
# mu = np.log(natural_mean ** 2 / np.sqrt(natural_sigma ** 2 + natural_mean ** 2))
# sigma = np.sqrt(np.log(natural_sigma ** 2 / (natural_mean ** 2) + 1))
# natural_mean * np.exp(stats.norm.ppf(np.random.uniform(0, 1)) * sigma ** 2), natural_mean + np.exp(
#     stats.norm.ppf(np.random.uniform(0, 1))
# ) * natural_sigma


class Treatment(PydanticModel):
    idx: int = conint(strict=True, ge=0, le=len(DISTRIBUTIONS) - 1)
    _mu: float = None
    _sigma: float = None
    _demand_rvs: List[float] = []

    class Config:
        extra = Extra.allow

    @classmethod
    def choose(cls) -> "Treatment":
        return Treatment(idx=random.choice(range(len(DISTRIBUTIONS))))

    @classmethod
    def from_json(cls, json: StrBytes) -> "Treatment":
        return Treatment.parse_raw(json)

    def get_optimal_order_quantity(self) -> float:
        rcpu, wcpu, scpu = self.get_unit_costs().tuple()
        overage_cost = wcpu - scpu
        underage_cost = rcpu - wcpu
        cf = float(underage_cost / (underage_cost + overage_cost))
        mu, sigma = self.get_distribution_parameters().tuple()

        # See "Mean" & "Variance" formulas: https://en.wikipedia.org/wiki/Log-normal_distribution
        natural_mean = np.exp(mu + (1 / 2) * sigma ** 2)
        natural_sigma = np.sqrt((np.exp(sigma ** 2) - 1) * np.exp(2 * mu + sigma ** 2))
        return float(natural_mean * np.exp(stats.norm.ppf(cf) * sigma))
        # return float(natural_mean + stats.norm.ppf(cf) * natural_sigma)  # TODO: might be negative... ask Anne

    def get_unit_costs(self) -> UnitCosts:
        return UnitCosts.from_treatment(self)

    def get_distribution_parameters(self) -> DisributionParameters:
        if self._mu is None or self._sigma is None:
            self._mu, self._sigma = DisributionParameters.from_treatment(self).tuple()
        return DisributionParameters(mu=self._mu, sigma=self._sigma)

    def get_demand_rvs(self, size: Optional[int] = None, disrupt: bool = False) -> List[float]:
        """Return samples from the applicable treatment distribution"""

        if size is None:
            size = int(1e4)
        assert type(size) is int and size > 0, f"""expected size to be a positive integer - got {size}"""

        if len(self._demand_rvs) == size and not disrupt:
            return self._demand_rvs

        mu, sigma = self.get_distribution_parameters().tuple()
        if disrupt:
            ## transform mu & sigma
            self._sigma *= 2
            self._mu *= 1
        self._demand_rvs = np.random.lognormal(self._mu, self._sigma, size).tolist()
        return self._demand_rvs
