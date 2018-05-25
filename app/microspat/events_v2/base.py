import eventlet
import flask

from app import socketio
from app.microspat.models import ArtifactEquation, ArtifactEstimator, LocusArtifactEstimator, \
    ArtifactEstimatorLocusParams, ArtifactEstimatorProject, Bin, BinEstimatorLocusParams, BinEstimatorProject, \
    Channel, Ladder, Plate, Well, GenotypingLocusParams, GenotypingProject, Locus, LocusSet, \
    ProjectChannelAnnotations, ProjectSampleAnnotations, QuantificationBiasEstimatorLocusParams, \
    QuantificationBiasEstimatorProject, Control, ControlSampleAssociation, Genotype, Sample, SampleLocusAnnotation, \
    LocusBinSet


class GetException(Exception):
    def __init__(self, *args):
        super(GetException, self).__init__(*args)


def respond_404(error):
    res = flask.jsonify({
        'error': error
    })
    res.status_code = 404
    return res


def emit_task_failure(task, message, namespace):
    socketio.emit(task, {
        'success': False,
        'message': message
    }, namespace=namespace)


def emit_task_success(task, message, namespace):
    socketio.emit(task, {
        'success': True,
        'message': message
    }, namespace=namespace)


def emit_list(model_namespace, schema_dump):
    socketio.emit('list', {model_namespace: schema_dump.data}, namespace=make_namespace(model_namespace))
    eventlet.sleep()


def emit_get(model_namespace, schema_dump):
    socketio.emit('get', {model_namespace: schema_dump.data}, namespace=make_namespace(model_namespace))
    eventlet.sleep()


def base_get(model, schema, namespace, subset_size=384):
    def get_fn(json):
        print("Base Get Request Received {}".format(namespace))
        ids = extract_ids(json)
        for id_subset in subset(ids, subset_size):
            instances = model.query.filter(model.id.in_(id_subset)).all()
            dump = schema.dumps(instances, many=True, separators=(',', ':'))
            res = {
                namespace: dump.data
            }
            socketio.emit('get', res, namespace=flask.request.namespace)
            eventlet.sleep()
    return get_fn


def base_list(model, schema, namespace, query=None):
    def list_fn():
        print("Base List Request Received {}".format(namespace))
        if not query:
            instances = model.query.all()
        else:
            instances = query()
        dump = schema.dumps(instances, many=True, separators=(',', ':'))
        res = {
            namespace: dump.data
        }
        socketio.emit('list', res, namespace=flask.request.namespace)
    return list_fn


def extract_ids(json):
    ids = json['id']
    try:
        if isinstance(ids, list):
            ids = list(map(int, ids))
        elif isinstance(ids, (int, str)):
            ids = [int(ids)]
        else:
            raise GetException("Field id not valid", json)
        return ids
    except ValueError:
        raise GetException("Field id not valid", json)


def subset(l, subset_size):
    i = 0
    total_elements = len(l)
    while i < total_elements:
        yield l[i: i + subset_size]
        i += subset_size


def make_namespace(string):
    return "/{}".format(string)


table_to_string_mapping = {
    ArtifactEquation: 'artifact_equation',
    ArtifactEstimator: 'artifact_estimator',
    LocusArtifactEstimator: 'locus_artifact_estimator',
    ArtifactEstimatorLocusParams: 'artifact_estimator_locus_params',
    ArtifactEstimatorProject: 'artifact_estimator_project',
    Bin: 'bin',
    BinEstimatorLocusParams: 'bin_estimator_locus_params',
    BinEstimatorProject: 'bin_estimator_project',
    LocusBinSet: 'locus_bin_set',
    Channel: 'channel',
    Ladder: 'ladder',
    Plate: 'plate',
    Well: 'well',
    GenotypingLocusParams: 'genotyping_locus_params',
    GenotypingProject: 'genotyping_project',
    Locus: 'locus',
    LocusSet: 'locus_set',
    ProjectChannelAnnotations: 'project_channel_annotations',
    ProjectSampleAnnotations: 'project_sample_annotations',
    QuantificationBiasEstimatorLocusParams: 'quantification_bias_estimator_locus_params',
    QuantificationBiasEstimatorProject: 'quantification_bias_estimator_project',
    Control: 'control',
    ControlSampleAssociation: 'control_sample_association',
    Genotype: 'genotype',
    Sample: 'sample',
    SampleLocusAnnotation: 'sample_locus_annotation'
}
