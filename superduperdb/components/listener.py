import dataclasses as dc
import typing as t

from overrides import override

from superduperdb import CFG
from superduperdb.backends.base.query import CompoundSelect
from superduperdb.base.datalayer import Datalayer
from superduperdb.base.document import _OUTPUTS_KEY
from superduperdb.misc.annotations import public_api
from superduperdb.misc.server import request_server

from ..jobs.job import Job
from .component import Component
from .model import Model


@public_api(stability='stable')
@dc.dataclass(kw_only=True)
class Listener(Component):
    """
    Listener object which is used to process a column/ key of a collection or table,
    and store the outputs.
    {component_parameters}
    :param key: Key to be bound to model
    :param model: Model for processing data
    :param select: Object for selecting which data is processed
    :param identifier: A string used to identify the model.
    :param active: Toggle to ``False`` to deactivate change data triggering
    :param predict_kwargs: Keyword arguments to self.model.predict
    """

    __doc__ = __doc__.format(component_parameters=Component.__doc__)

    key: t.Union[str, t.List, t.Dict]
    model: t.Union[str, Model]
    select: CompoundSelect
    identifier: t.Optional[str] = None  # type: ignore[assignment]
    active: bool = True
    predict_kwargs: t.Optional[t.Dict] = dc.field(default_factory=dict)

    type_id: t.ClassVar[str] = 'listener'

    def __post_init__(self, artifacts):
        if self.identifier is None and self.model is not None:
            if isinstance(self.model, str):
                self.identifier = f'{self.model}/{self.id_key}'
            else:
                self.identifier = f'{self.model.identifier}/{self.id_key}'
        super().__post_init__(artifacts)

    @property
    def outputs(self):
        return (
            f'{_OUTPUTS_KEY}.{self.id_key}.{self.model.identifier}.{self.model.version}'
        )

    @override
    def pre_create(self, db: Datalayer) -> None:
        if isinstance(self.model, str):
            self.model = t.cast(Model, db.load('model', self.model))

        if self.select is not None and self.select.variables:
            self.select = t.cast(CompoundSelect, self.select.set_variables(db))

    def post_create(self, db: Datalayer) -> None:
        # Start cdc service if enabled
        if self.select is not None and self.active and not db.server_mode:
            if CFG.cluster.cdc.uri:
                request_server(
                    service='cdc',
                    endpoint='listener/add',
                    args={'name': self.identifier},
                    type='get',
                )
            else:
                db.cdc.add(self)

    @property
    def dependencies(self) -> t.List[str]:
        out = []

        def check_dependable(k):
            nonlocal out
            if k.startswith('_outputs.'):
                _, key, model = k.split('.')
                out.append(f'{model}/{key}')

        if isinstance(self.key, str):
            check_dependable(self.key)

        elif isinstance(self.key, (tuple, list)):
            for k in self.key:
                check_dependable(k)

        elif isinstance(self.key, dict):
            for k in self.key.values():
                check_dependable(k)

        if self.select.output_fields:
            out.extend([f'{v}/{k}' for k, v in self.select.output_fields.items()])
        return out

    @property
    def id_key(self) -> str:
        def _id_key(key) -> str:
            if isinstance(key, str):
                if key.startswith('_outputs.'):
                    return key.split('.')[1]
                else:
                    return key
            elif isinstance(key, (tuple, list)):
                return ','.join([_id_key(k) for k in key])
            elif isinstance(key, dict):
                return ','.join([_id_key(k) for k in key.values()])
            else:
                raise TypeError('Type of key is not valid')

        return _id_key(self.key)

    @override
    def schedule_jobs(
        self,
        db: Datalayer,
        dependencies: t.Sequence[Job] = (),
        verbose: bool = False,
    ) -> t.Sequence[t.Any]:
        """
        Schedule jobs for the listener

        :param database: The DB instance to process
        :param dependencies: A list of dependencies
        :param verbose: Whether to print verbose output
        """
        if not self.active:
            return []
        assert not isinstance(self.model, str)

        out = [
            self.model.predict(
                X=self.key,
                db=db,
                select=self.select,
                dependencies=dependencies,
                **(self.predict_kwargs or {}),
            )
        ]
        return out

    def cleanup(self, database: Datalayer) -> None:
        """Clean up when the listener is deleted

        :param database: The DB instance to process
        """
        # TODO - this doesn't seem to do anything
        if (cleanup := getattr(self.select, 'model_cleanup', None)) is not None:
            assert not isinstance(self.model, str)
            cleanup(database, model=self.model.identifier, key=self.key)
