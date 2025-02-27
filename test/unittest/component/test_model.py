import inspect
import random
from test.db_config import DBConfig
from unittest.mock import MagicMock, patch

import bson
import numpy as np
import pytest
from sklearn.metrics import accuracy_score, f1_score

from superduperdb.backends.base.query import CompoundSelect, Select
from superduperdb.backends.local.compute import LocalComputeBackend
from superduperdb.backends.mongodb.query import Collection
from superduperdb.base.datalayer import Datalayer
from superduperdb.base.document import Document
from superduperdb.base.serializable import Variable
from superduperdb.components.component import Component
from superduperdb.components.dataset import Dataset
from superduperdb.components.datatype import DataType
from superduperdb.components.listener import Listener
from superduperdb.components.metric import Metric
from superduperdb.components.model import (
    Model,
    QueryModel,
    SequentialModel,
    _Predictor,
    _TrainingConfiguration,
)

# ------------------------------------------
# Test the _TrainingConfiguration class (tc)
# ------------------------------------------


def test_tc_type_id():
    config = _TrainingConfiguration('config')
    assert config.type_id == 'training_configuration'


def test_tc_get_method():
    config = _TrainingConfiguration('config', kwargs={'param1': 'value1'})
    config.version = 1

    assert config.get('identifier') == 'config'
    assert config.get('param1') == 'value1'
    assert config.get('version') == 1

    assert config.get('non_existent') is None
    assert config.get('non_existent', 'default_value') == 'default_value'

    # First get the properties of the instance
    config = _TrainingConfiguration('config', kwargs={'version': 2})
    config.version = 1
    assert config.get('version') == 1


# --------------------------------
# Test the PredictMixin class (pm)
# --------------------------------


def return_self(x):
    return x


def return_self_multikey(x, y, z):
    return [x, y, z]


def to_call(x):
    if isinstance(x, list):
        return [to_call(i) for i in x]
    return x * 5


def to_call_multi(x):
    if isinstance(x[0], list):
        return [1] * len(x)
    return 1


def preprocess(x):
    return x + 1


def preprocess_multi(x, y):
    return x + y


def postprocess(x):
    return x + 0.1


def mock_forward(self, x, **kwargs):
    return to_call(x)


def mock_forward_multi(self, x, **kwargs):
    return to_call_multi(x)


class TestModel(Component, _Predictor):
    batch_predict: bool = False


@pytest.fixture
def predict_mixin(request) -> _Predictor:
    cls_ = getattr(request, 'param', _Predictor)

    if 'identifier' in inspect.signature(cls_).parameters:
        predict_mixin = cls_(identifier='test')
    else:
        predict_mixin = cls_()
    predict_mixin.identifier = 'test'
    predict_mixin.to_call = to_call
    predict_mixin.preprocess = preprocess
    predict_mixin.postprocess = postprocess
    predict_mixin.takes_context = False
    predict_mixin.output_schema = None
    predict_mixin.datatype = None
    predict_mixin.model_update_kwargs = {}
    predict_mixin.version = 0
    return predict_mixin


@pytest.fixture
def predict_mixin_multikey(request) -> _Predictor:
    cls_ = getattr(request, 'param', _Predictor)

    if 'identifier' in inspect.signature(cls_).parameters:
        predict_mixin = cls_(identifier='test')
    else:
        predict_mixin = cls_()
    predict_mixin.identifier = 'test'
    predict_mixin.to_call = to_call_multi
    predict_mixin.preprocess = preprocess_multi
    predict_mixin.postprocess = postprocess
    predict_mixin.takes_context = False
    predict_mixin.output_schema = None
    predict_mixin.datatype = None
    predict_mixin.model_update_kwargs = {}
    predict_mixin.version = 0
    return predict_mixin


def test_pm_predict_one(predict_mixin):
    X = np.random.randn(5)

    # preprocess -> to_call -> postprocess
    expect = postprocess(to_call(preprocess(X)))
    assert np.allclose(predict_mixin._predict_one(X), expect)

    # to_call -> postprocess
    with patch.object(predict_mixin, 'preprocess', None):
        expect = postprocess(to_call(X))
        assert np.allclose(predict_mixin._predict_one(X), expect)

    # preprocess -> to_call
    with patch.object(predict_mixin, 'postprocess', None):
        expect = to_call(preprocess(X))
        assert np.allclose(predict_mixin._predict_one(X), expect)


@pytest.mark.parametrize(
    'batch_predict, num_workers, expect_type',
    [
        [True, 0, np.ndarray],
        [False, 0, list],
        [False, 1, list],
        [False, 5, list],
    ],
)
def test_pm_forward(batch_predict, num_workers, expect_type):
    predict_mixin = _Predictor()
    X = np.random.randn(4, 5)

    predict_mixin.to_call = to_call
    predict_mixin.batch_predict = batch_predict

    output = predict_mixin._forward(X, num_workers=num_workers)
    assert isinstance(output, expect_type)
    assert np.allclose(output, to_call(X))


@patch.object(_Predictor, '_forward', mock_forward_multi)
def test_predict_core_multikey(predict_mixin_multikey):
    X = 1
    Y = 2
    Z = 2

    # Multi key with preprocess
    # As list
    predict_mixin_multikey.preprocess = None
    expect = postprocess(to_call_multi([X, Y, Z]))
    output = predict_mixin_multikey._predict([[X, Y, Z], [X, Y, Z]])
    assert isinstance(output, list)
    assert np.allclose(output, expect)


@patch.object(_Predictor, '_forward', mock_forward)
def test_predict_core_multikey_dict(predict_mixin_multikey):
    X = 1
    Y = 2
    # As Dict
    predict_mixin_multikey.preprocess = preprocess_multi
    output = predict_mixin_multikey._predict([{'x': X, 'y': Y}])
    assert isinstance(output, list)
    assert np.allclose(output, 15.1)


@patch.object(_Predictor, '_forward', mock_forward)
def test_predict_preprocess_multikey(predict_mixin_multikey):
    X = 1
    Y = 2

    # Multi key with preprocess
    predict_mixin_multikey.to_call = to_call
    expect = postprocess(to_call(preprocess_multi(X, Y)))
    output = predict_mixin_multikey._predict([[X, Y], [X, Y]])
    assert isinstance(output, list)
    assert np.allclose(output, expect)


@patch.object(_Predictor, '_forward', mock_forward)
def test_pm_core_predict(predict_mixin):
    X = np.random.randn(4, 5)

    # make sure _predict_one is called
    with patch.object(predict_mixin, '_predict_one', return_self):
        assert predict_mixin._predict(5, one=True) == return_self(5)

    expect = postprocess(to_call(preprocess(X)))
    output = predict_mixin._predict(X)
    assert isinstance(output, list)
    assert np.allclose(output, expect)

    # to_call -> postprocess
    with patch.object(predict_mixin, 'preprocess', None):
        expect = postprocess(to_call(X))
        output = predict_mixin._predict(X)
        assert isinstance(output, list)
        assert np.allclose(output, expect)

    # preprocess -> to_call
    with patch.object(predict_mixin, 'postprocess', None):
        output = predict_mixin._predict(X)
        expect = to_call(preprocess(X))
        assert isinstance(output, list)
        assert np.allclose(output, expect)


def test_pm_create_predict_job(predict_mixin):
    select = MagicMock(spec=Select)
    X = 'x'
    ids = [1, 2, 3]
    max_chunk_size = 2
    job = predict_mixin.create_predict_job(X, select, ids, max_chunk_size)
    assert job.component_identifier == predict_mixin.identifier
    assert job.method_name == 'predict'
    assert job.args == [X]
    assert job.kwargs['max_chunk_size'] == max_chunk_size
    assert job.kwargs['ids'] == ids


@patch.object(Datalayer, 'add')
@pytest.mark.parametrize("db", [DBConfig.mongodb_empty], indirect=True)
def test_pm_predict_and_listen(mock_add, predict_mixin, db):
    X = 'x'
    select = MagicMock(CompoundSelect)

    in_memory = False
    max_chunk_size = 2
    predict_mixin._predict_and_listen(
        X,
        select,
        db=db,
        max_chunk_size=max_chunk_size,
        in_memory=in_memory,
    )
    listener = mock_add.call_args[0][0]

    # Check whether create a correct listener
    assert isinstance(listener, Listener)
    assert listener.model == predict_mixin
    assert listener.predict_kwargs['in_memory'] == in_memory
    assert listener.predict_kwargs['max_chunk_size'] == max_chunk_size


@pytest.mark.parametrize('predict_mixin', [TestModel], indirect=True)
def test_pm_predict(predict_mixin):
    # Check the logic of predict method, the mock method will be tested below
    db = MagicMock(spec=Datalayer)
    db.compute = MagicMock(spec=LocalComputeBackend)
    db.metadata = MagicMock()
    select = MagicMock(spec=Select)
    select.table_or_collection = MagicMock()

    with patch.object(predict_mixin, '_predict_and_listen') as predict_func:
        predict_mixin.predict('x', db, select, listen=True)
        predict_func.assert_called_once()

    with patch.object(predict_mixin, '_predict') as predict_func:
        predict_mixin.predict('x')
        predict_func.assert_called_once()


def test_pm_predict_with_select(predict_mixin):
    # Check the logic about overwrite in _predict_with_select
    X = 'x'
    all_ids = ['1', '2', '3']
    ids_of_missing_outputs = ['1', '2']

    select = MagicMock(spec=Select)
    select.select_ids_of_missing_outputs.return_value = 'missing'

    def return_value(select_type):
        ids = ids_of_missing_outputs if select_type == 'missing' else all_ids
        query_result = [
            (
                {
                    'id_field': id,
                }
            )
            for id in ids
        ]
        return query_result

    db = MagicMock(spec=Datalayer)
    db.execute.side_effect = return_value
    db.databackend = MagicMock()
    db.databackend.id_field = 'id_field'

    # overwrite = True
    with patch.object(predict_mixin, '_predict_with_select_and_ids') as mock_predict:
        predict_mixin._predict_with_select(X, select, db, overwrite=True)
        _, kwargs = mock_predict.call_args
        assert kwargs.get('ids') == all_ids

    # overwrite = False
    with patch.object(predict_mixin, '_predict_with_select_and_ids') as mock_predict:
        predict_mixin._predict_with_select(
            X, select, db, overwrite=False, max_chunk_size=None, in_memory=True
        )
        _, kwargs = mock_predict.call_args
        assert kwargs.get('ids') == ids_of_missing_outputs


def test_model_on_create():
    db = MagicMock(spec=Datalayer)
    db.databackend = MagicMock()

    # Check the encoder is loaded if encoder is string
    model = Model('test', object=object(), datatype='test_encoder')
    with patch.object(db, 'load') as db_load:
        model.pre_create(db)
        db_load.assert_called_with('datatype', 'test_encoder')

    # Check the output_component table is added by datalayer
    model = Model('test', object=object(), datatype=DataType(identifier='test'))
    output_component = MagicMock()
    db.databackend.create_model_table_or_collection.return_value = output_component
    with patch.object(db, 'add') as db_load:
        model.post_create(db)
        db_load.assert_called_with(output_component)


def test_model_append_metrics():
    model = Model('test', object=object())

    metric_values = {'acc': 0.5, 'loss': 0.5}

    model.append_metrics(metric_values)

    assert model.metric_values.get('acc') == [0.5]
    assert model.metric_values.get('loss') == [0.5]

    metric_values = {'acc': 0.6, 'loss': 0.4}
    model.append_metrics(metric_values)
    assert model.metric_values.get('acc') == [0.5, 0.6]
    assert model.metric_values.get('loss') == [0.5, 0.4]


@patch.object(Model, '_validate')
def test_model_validate(mock_validate):
    # Check the metadadata recieves the correct values
    mock_validate.return_value = {'acc': 0.5, 'loss': 0.5}
    model = Model('test', object=object())
    db = MagicMock(spec=Datalayer)
    db.metadata = MagicMock()
    with patch.object(db, 'add') as db_add, patch.object(
        db.metadata, 'update_object'
    ) as update_object:
        model.validate(db, MagicMock(spec=Dataset), [MagicMock(spec=Metric)])
        db_add.assert_called_once_with(model)
        _, kwargs = update_object.call_args
        assert kwargs.get('key') == 'dict.metric_values'
        assert kwargs.get('value') == {'acc': 0.5, 'loss': 0.5}


@patch.object(Model, '_predict')
@pytest.mark.parametrize(
    "db",
    [
        (DBConfig.mongodb_data, {'n_data': 500}),
        (DBConfig.sqldb_data, {'n_data': 500}),
    ],
    indirect=True,
)
def test_model_core_validate(model_predict, valid_dataset, db):
    # Check the validation is done correctly
    db.add(valid_dataset)
    model = Model('test', object=object(), train_X='x', train_y='y')
    model_predict.side_effect = lambda x: [random.randint(0, 1) for _ in range(len(x))]
    metrics = [
        Metric('f1', object=f1_score),
        Metric('acc', object=accuracy_score),
    ]
    results = model._validate(db, valid_dataset.identifier, metrics)
    assert len(results) == 2
    assert isinstance(results.get(f'{valid_dataset.identifier}/f1'), float)
    assert isinstance(results.get(f'{valid_dataset.identifier}/acc'), float)

    results = model._validate(db, valid_dataset, metrics)
    assert len(results) == 2
    assert isinstance(results.get(f'{valid_dataset.identifier}/f1'), float)
    assert isinstance(results.get(f'{valid_dataset.identifier}/acc'), float)


def test_model_create_fit_job():
    # Check the fit job is created correctly
    model = Model('test', object=object())
    job = model.create_fit_job('x')
    assert job.component_identifier == model.identifier
    assert job.method_name == 'fit'
    assert job.args == ['x']


def test_model_fit(valid_dataset):
    # Check the logic of the fit method, the mock method was tested above
    model = Model('test', object=object())
    with patch.object(model, '_fit') as model_fit:
        model.fit('x')
        model_fit.assert_called_once()

    with patch.object(model, '_fit') as model_fit:
        db = MagicMock(spec=Datalayer)
        db.compute = MagicMock(spec=LocalComputeBackend)
        model.fit(
            valid_dataset,
            db=db,
            validation_sets=[valid_dataset],
        )
        _, kwargs = model_fit.call_args
        assert kwargs.get('validation_sets') == [valid_dataset.identifier]


@pytest.mark.parametrize(
    "db",
    [
        (DBConfig.mongodb, {'n_data': 500}),
    ],
    indirect=True,
)
def test_query_model(db):
    q = (
        Collection(identifier='documents')
        .like({'x': Variable('X')}, vector_index='test_vector_search', n=3)
        .find_one({}, {'_id': 1})
    )

    # check = q.set_variables(db, X='test')

    m = QueryModel(
        identifier='test-query-model',
        select=q,
        postprocess=lambda r: r['_id'],
    )
    m.db = db

    import torch

    out = m.predict(X=torch.randn(32), one=True)

    assert isinstance(out, bson.ObjectId)

    out = m.predict(X=torch.randn(4, 32))

    assert len(out) == 4

    db.add(m)

    n = db.load('model', m.identifier)
    assert set(x.value for x in n.select.variables) == set(x.value for x in q.variables)


def test_sequential_model():
    m = SequentialModel(
        identifier='test-sequential-model',
        predictors=[
            Model(
                identifier='test-predictor-1',
                object=lambda x: x + 2,
            ),
            Model(
                identifier='test-predictor-2',
                object=lambda x: x + 1,
            ),
        ],
    )

    assert m.predict(X=1, one=True) == 4
    assert m.predict(X=[1, 1, 1, 1]) == [4, 4, 4, 4]


@patch.object(_Predictor, '_predict')
def test_pm_predict_with_select_ids(
    predict_mock, predict_mixin_multikey, predict_mixin
):
    def _test(multi_key, predict_mixin_multikey):
        xs = [np.random.randn(4) for _ in range(10)]
        ys = [int(random.random() > 0.5) for i in range(10)]
        if multi_key:
            docs = [Document({'x': x, 'y': x, 'z': x}) for x in xs]
            X = ['x', 'y', 'z']
        else:
            docs = [Document({'x': x}) for x in xs]
            X = 'x'

        ids = [i for i in range(10)]

        select = MagicMock(spec=Select)
        db = MagicMock(spec=Datalayer)
        db.execute.return_value = docs

        # Check the base predict function
        predict_mock.return_value = ys
        predict_mixin_multikey.db = db
        with patch.object(select, 'select_using_ids') as select_using_ids, patch.object(
            select, 'model_update'
        ) as model_update:
            predict_mixin_multikey._predict_with_select_and_ids(X, db, select, ids)
            select_using_ids.assert_called_once_with(ids)
            _, kwargs = model_update.call_args
            #  make sure the outputs are set
            assert kwargs.get('outputs') == ys

        # Check the base predict function with encoder
        from superduperdb.components.datatype import DataType

        predict_mixin_multikey.datatype = DataType(identifier='test')
        with patch.object(select, 'model_update') as model_update:
            predict_mixin_multikey._predict_with_select_and_ids(X, db, select, ids)
            select_using_ids.assert_called_once_with(ids)
            _, kwargs = model_update.call_args
            #  make sure encoder is used
            datatype = predict_mixin_multikey.datatype
            assert kwargs.get('outputs') == [datatype(y).encode() for y in ys]

        # Check the base predict function with output_schema
        from superduperdb.components.schema import Schema

        predict_mixin_multikey.datatype = None
        predict_mixin_multikey.output_schema = schema = MagicMock(spec=Schema)
        schema.side_effect = str
        predict_mock.return_value = [{'y': y} for y in ys]
        with patch.object(select, 'model_update') as model_update:
            predict_mixin_multikey._predict_with_select_and_ids(X, db, select, ids)
            select_using_ids.assert_called_once_with(ids)
            _, kwargs = model_update.call_args
            assert kwargs.get('outputs') == [str({'y': y}) for y in ys]

    # Test multikey
    _test(1, predict_mixin_multikey)

    # Test single key
    _test(0, predict_mixin)


@pytest.mark.parametrize(
    "db",
    [
        (DBConfig.mongodb_empty, {}),
    ],
    indirect=True,
)
def test_predict_insert(db):
    # Check that when `insert_to` is specified, then the input
    #  and output of the prediction are saved in the database

    m = Model(
        identifier='test-predictor-1',
        object=lambda x: x + 2,
    )

    db.add(m)
    m.predict(
        X=Document({'x': 1}), key='x', one=True, insert_to=Collection('documents')
    )
    r = db.execute(Collection('documents').find_one())
    out = r['_outputs']['x'][m.identifier]['0']
    assert out == 3
