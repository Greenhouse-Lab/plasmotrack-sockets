import csv
import os
from collections import defaultdict
from datetime import datetime

from sklearn.externals import joblib
from sqlalchemy.ext.declarative import declared_attr
from sqlalchemy.orm import validates, deferred, reconstructor
from sqlalchemy import event
from sqlalchemy.orm.util import object_state
from sqlalchemy.orm.session import attributes

from app import db
from config import Config
from fsa_extractor.PlateExtractor import PlateExtractor, WellExtractor, ChannelExtractor
import bin_finder.BinFinder as BF
import artifact_estimator.ArtifactEstimator as AE

from app.custom_sql_types.custom_types import JSONEncodedData, MutableDict, MutableList
from app.plasmomapper.peak_annotator.PeakFilters import base_size_filter, bleedthrough_filter, crosstalk_filter, \
    peak_height_filter, peak_proximity_filter, relative_peak_height_filter


def params_changed(target, params):
    state = object_state(target)
    if not state.modified:
        return False

    dict_ = state.dict

    for attr in state.manager.attributes:
        if not hasattr(attr.impl, 'get_history') or hasattr(attr.impl, 'get_collection') or attr.key not in params:
            continue
        (added, unchanged, deleted) = attr.impl.get_history(state, dict_, passive=attributes.NO_CHANGE)
        if added or deleted:
            return True
    else:
        return False


class Colored(object):
    color = db.Column(db.String(6), nullable=False)

    @validates('color')
    def validate_color(self, key, color):
        assert color in ['orange', 'red', 'yellow', 'green', 'blue']
        return color


class PeakScanner(object):
    scanning_method = db.Column(db.Text, default='relmax', nullable=False)
    maxima_window = db.Column(db.Integer, default=10, nullable=False)

    # relmax Scanning Params
    argrelmax_window = db.Column(db.Integer, default=6, nullable=False)
    trace_smoothing_window = db.Column(db.Integer, default=11, nullable=False)
    trace_smoothing_order = db.Column(db.Integer, default=7, nullable=False)
    tophat_factor = db.Column(db.Float, default=.005, nullable=False)

    # CWT Scanning Params
    cwt_min_width = db.Column(db.Integer, default=4, nullable=False)
    cwt_max_width = db.Column(db.Integer, default=15, nullable=False)
    min_snr = db.Column(db.Float, default=3, nullable=False)
    noise_perc = db.Column(db.Float, default=13, nullable=False)

    @validates('scanning_method')
    def validate_scanning_method(self, key, scanning_method):
        assert scanning_method in ['cwt', 'relmax']
        return scanning_method

    @property
    def scanning_parameters(self):
        return {
            'scanning_method': self.scanning_method,
            'maxima_window': self.maxima_window,
            'argrelmax_window': self.argrelmax_window,
            'trace_smoothing_window': self.trace_smoothing_window,
            'trace_smoothing_order': self.trace_smoothing_order,
            'tophat_factor': self.tophat_factor,
            'cwt_min_width': self.cwt_min_width,
            'cwt_max_width': self.cwt_max_width,
            'min_snr': self.min_snr,
            'noise_perc': self.noise_perc
        }


class TimeStamped(object):
    last_updated = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Flaggable(object):
    flags = db.Column(MutableDict.as_mutable(JSONEncodedData), default={}, nullable=False)
    comments = db.Column(db.Text)


class LocusSetAssociatedMixin(object):
    @declared_attr
    def locus_set_id(self):
        return db.Column(db.Integer, db.ForeignKey('locus_set.id'), nullable=False)

    @declared_attr
    def locus_set(self):
        return db.relationship('LocusSet', cascade='save-update, merge')


class SkLearnModel(object):
    def __init__(self):
        if not os.path.exists(os.path.join(Config.PLASMOMAPPER_BASEDIR, 'pickled_models', type(self).__name__)):
            os.mkdir(os.path.join(Config.PLASMOMAPPER_BASEDIR, 'pickled_models', type(self).__name__))

        if self.id and os.path.exists(self.model_location):
            self.model = joblib.load(self.model_location)
        else:
            self.model = None

    @property
    def model_location(self):
        return os.path.join(Config.PLASMOMAPPER_BASEDIR, 'pickled_models', type(self).__name__, str(self.id) + ".pkl")

    def save_model(self, model):
        if self.id:
            self.model = model
            joblib.dump(self.model, self.model_location)
        else:
            raise AttributeError("{} has not yet been persisted to database.".format(type(self).__name__))


class Sample(TimeStamped, Flaggable, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    barcode = db.Column(db.String(255), nullable=False, unique=True)
    designation = db.Column(db.String(255), nullable=False, default='sample')
    channels = db.relationship('Channel', backref=db.backref('sample'), lazy='dynamic')

    @validates('designation')
    def validate_designation(self, key, designation):
        assert designation in ['sample', 'positive_control', 'negative_control']
        return designation

    def serialize(self):
        return {
            'id': self.id,
            'barcode': self.barcode,
            'comments': self.comments,
            'designation': self.designation,
            'last_updated': str(self.last_updated)
        }


class Project(LocusSetAssociatedMixin, TimeStamped, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(255), unique=True, nullable=False)
    date = db.Column(db.DateTime, default=datetime.utcnow)
    creator = db.Column(db.String(255))
    description = db.Column(db.Text, nullable=True)

    channel_annotations = db.relationship('ProjectChannelAnnotations', backref=db.backref('project'), lazy='select',
                                          cascade='save-update, merge, delete, delete-orphan')

    @property
    def locus_parameters(self):
        raise NotImplementedError("Project should not be directly initialized.")

    discriminator = db.Column('type', db.String(255))
    __mapper_args__ = {'polymorphic_on': discriminator,
                       'polymorphic_identity': 'base_project'}

    def __init__(self, locus_set_id, **kwargs):
        super(Project, self).__init__(**kwargs)
        locus_set = LocusSet.query.get(locus_set_id)
        self.locus_set = locus_set
        assert isinstance(locus_set, LocusSet)
        for locus in locus_set.loci:
            assert isinstance(locus, Locus)
            locus_param = self.__class__.locus_parameters.mapper.class_()
            locus_param.locus = locus
            self.locus_parameters.append(locus_param)

    def __repr__(self):
        return "<{} {}>".format(self.__class__.__name__, self.title)

    @validates('locus_parameters')
    def validate_locus_params(self, key, locus_param):
        assert locus_param.locus in self.locus_set.loci
        return locus_param

    def add_channel(self, channel_id, block_commit=False):

        # channel = Channel.query.get(channel_id)

        channel_locus_id = Channel.query.filter(Channel.id == channel_id).value(Channel.locus_id)

        if not channel_locus_id:
            raise ValueError("Channel does not have a locus assigned.")

        valid_locus_id = Project.query.join(LocusSet).join(locus_set_association_table).join(Locus).filter(
            Project.id == self.id).filter(Locus.id == channel_locus_id).first()

        if not valid_locus_id:
            raise ValueError(
                "Channel locus {} is not a member of this project's analysis set.".format(
                    channel_locus_id)
            )

        channel_annotation = self.create_channel_annotation(channel_id)

        locus_parameters = self.get_locus_parameters(channel_locus_id)

        # if not locus_parameters.scanning_parameters_stale and not locus_parameters.filter_parameters_stale:
        #     self.recalculate_channel(channel_annotation.id, rescan_peaks=True, block_commit=block_commit)
        locus_parameters.scanning_parameters_stale = True
        locus_parameters.filter_parameters_stale = True
        if not block_commit:
            db.session.commit()

        return channel_annotation

    def add_channels(self, channel_ids, block_commit=False):
        channel_annotations = []

        for channel_id in channel_ids:
            channel_locus_id = Channel.query.filter(Channel.id == channel_id).value(Channel.locus_id)

            if not channel_locus_id:
                raise ValueError("Channel does not have a locus assigned.")

            valid_locus_id = Project.query.join(LocusSet).join(locus_set_association_table).join(Locus).filter(
                Project.id == self.id).filter(Locus.id == channel_locus_id).first()

            if not valid_locus_id:
                raise ValueError(
                    "Channel locus {} is not a member of this project's analysis set.".format(
                        channel_locus_id)
                )

            # channel_annotation = self.create_channel_annotation(channel_id)

            locus_parameters = self.get_locus_parameters(channel_locus_id)

            # if not locus_parameters.scanning_parameters_stale and not locus_parameters.filter_parameters_stale:
            #     self.recalculate_channel(channel_annotation.id, rescan_peaks=True, block_commit=block_commit)
            locus_parameters.scanning_parameters_stale = True
            locus_parameters.filter_parameters_stale = True

        # for channel_id in channel_ids:
        #     channel_annotations.append(self.add_channel(channel_id, block_commit=True))

        self.bulk_create_channel_annotations(channel_ids, block_commit=True)

        if not block_commit:
            db.session.commit()

        return channel_annotations

    def create_channel_annotation(self, channel_id):
        channel_annotation = ProjectChannelAnnotations()
        channel_annotation.channel_id = channel_id
        self.channel_annotations.append(channel_annotation)
        return channel_annotation

    def bulk_create_channel_annotations(self, channel_ids, block_commit=False):
        objs = []
        for channel_id in channel_ids:
            objs.append(ProjectChannelAnnotations(channel_id=channel_id, project_id=self.id))
        db.session.bulk_save_objects(objs)
        if not block_commit:
            db.session.commit()
        return objs

    def recalculate_channel(self, channel_annotation_id, rescan_peaks, block_commit=False):
        channel_annotation = ProjectChannelAnnotations.query.get(channel_annotation_id)
        channel = channel_annotation.channel
        if channel.well.base_sizes:
            filter_params = self.get_filter_parameters(channel.locus_id)
            if rescan_peaks:
                scanning_params = self.get_scanning_parameters(channel.locus_id)
                channel.identify_peak_indices(scanning_params)
                channel_annotation.peak_indices = channel.peak_indices
            else:
                channel.set_peak_indices(channel_annotation.peak_indices)
            channel.pre_annotate_and_filter(filter_params)

            total_peaks = -1
            while len(channel.peaks) != total_peaks:
                channel.post_annotate_peaks()
                channel.post_filter_peaks(filter_params)
                total_peaks = len(channel.peaks)

            channel_annotation.annotated_peaks = channel.peaks[:]

        if not block_commit:
            db.session.commit()

        return channel_annotation

    def recalculate_channels(self, channel_annotation_ids, rescan_peaks, block_commit=False):
        channel_annotations = []
        for channel_annotation_id in channel_annotation_ids:
            channel_annotations.append(self.recalculate_channel(channel_annotation_id, rescan_peaks, block_commit=True))

        if not block_commit:
            db.session.commit()

        return channel_annotations

    def get_filter_parameters(self, locus_id):
        return self.get_locus_parameters(locus_id).filter_parameters

    def get_scanning_parameters(self, locus_id):
        return self.get_locus_parameters(locus_id).scanning_parameters

    def recalculate_locus(self, locus_id, block_commit=False):
        locus_parameters = self.get_locus_parameters(locus_id)
        assert isinstance(locus_parameters, ProjectLocusParams)
        print locus_parameters
        print locus_parameters.scanning_parameters_stale
        print locus_parameters.filter_parameters_stale
        print "Scanning Parameters Stale: {}".format(locus_parameters.scanning_parameters_stale)
        print "Filter Parameters Stale: {}".format(locus_parameters.filter_parameters_stale)
        channel_annotations = self.get_locus_channel_annotations(locus_id)

        if locus_parameters.scanning_parameters_stale:
            channel_annotations = self.recalculate_channels(channel_annotation_ids=[_.id for _ in channel_annotations],
                                                            rescan_peaks=True, block_commit=True)
        else:
            if locus_parameters.filter_parameters_stale:
                channel_annotations = self.recalculate_channels(
                    channel_annotation_ids=[_.id for _ in channel_annotations],
                    rescan_peaks=False)

        locus_parameters.scanning_parameters_stale = False
        locus_parameters.filter_parameters_stale = False

        if not block_commit:
            db.session.commit()

        return channel_annotations

    def analyze_locus(self, locus_id, block_commit=False):
        print "Recalculating Locus"
        self.recalculate_locus(locus_id, block_commit=block_commit)
        return self

    def get_locus_channel_annotations(self, locus_id):
        return ProjectChannelAnnotations.query.join(Channel).filter(
            ProjectChannelAnnotations.project_id == self.id).filter(Channel.locus_id == locus_id).all()

    def get_locus_parameters(self, locus_id):
        return ProjectLocusParams.query.filter(ProjectLocusParams.locus_id == locus_id).filter(
            ProjectLocusParams.project_id == self.id).one()

    def serialize(self):
        return {
            'id': self.id,
            'title': self.title,
            'date': str(self.date),
            'creator': self.creator,
            'description': self.description,
            'last_updated': str(self.last_updated),
            'locus_set_id': self.locus_set_id,
            'locus_parameters': {},
        }

    def serialize_details(self):
        res = self.serialize()
        res.update({
            'locus_parameters': {locus_param.locus_id: locus_param.serialize() for locus_param in
                                 self.locus_parameters}
        })
        return res


class BinEstimating(object):
    @declared_attr
    def bin_estimator_id(self):
        return db.Column(db.Integer, db.ForeignKey('bin_estimator_project.id'), index=True)

    @declared_attr
    def bin_estimator(self):
        return db.relationship('BinEstimatorProject', lazy='immediate', foreign_keys=[self.bin_estimator_id])

    def bin_estimator_changed(self):
        raise NotImplementedError()

        # artifact_estimator_id = db.Column(db.Integer, db.ForeignKey('artifact_estimator_project.id'))
        # artifact_estimator = db.relationship('ArtifactEstimatorProject', foreign_keys=[artifact_estimator_id],
        #                                      lazy='immediate')


class ArtifactEstimating(object):
    @declared_attr
    def artifact_estimator_id(self):
        return db.Column(db.Integer, db.ForeignKey('artifact_estimator_project.id'), index=True)

    @declared_attr
    def artifact_estimator(self):
        return db.relationship('ArtifactEstimatorProject', lazy='immediate', foreign_keys=[self.artifact_estimator_id])

    def artifact_estimator_changed(self):
        raise NotImplementedError()


class SampleBasedProject(Project):
    __mapper_args__ = {'polymorphic_identity': 'sample_based_project'}

    @declared_attr
    def sample_annotations(self):
        return db.relationship('ProjectSampleAnnotations', backref=db.backref('project'), lazy='dynamic',
                               cascade='save-update, merge, delete, delete-orphan')

    def add_sample(self, sample_id, block_commit=False):
        sample_annotation = ProjectSampleAnnotations(sample_id=sample_id)
        self.sample_annotations.append(sample_annotation)

        channel_ids = Channel.query.join(Sample).join(Locus).join(locus_set_association_table).join(LocusSet).join(
            Project).filter(
            Project.id == self.id).filter(Sample.id == sample_id).values(Channel.id)

        self.add_channels([str(x[0]) for x in channel_ids], block_commit=True)

        if not block_commit:
            db.session.commit()

        return sample_annotation

    def add_samples(self, sample_ids):
        full_sample_ids = sample_ids
        n = 0
        while n * 100 < len(full_sample_ids):
            sample_ids = full_sample_ids[n * 100: (n + 1) * 100]
            channel_ids_query = Channel.query.join(Sample).join(Locus).join(locus_set_association_table).join(
                LocusSet).join(Project).filter(Project.id == self.id)
            channel_ids = []
            for sample_id in sample_ids:
                channel_ids += [x[0] for x in channel_ids_query.filter(Sample.id == sample_id).values(Channel.id)]
                sample_annotation = ProjectSampleAnnotations(sample_id=sample_id)
                self.sample_annotations.append(sample_annotation)
                for locus in self.locus_set.loci:
                    locus_sample_annotation = SampleLocusAnnotation(locus_id=locus.id)
                    bin_ids = Bin.query.join(LocusBinSet).join(BinEstimatorProject).filter(
                        BinEstimatorProject.id == self.bin_estimator_id).filter(
                        LocusBinSet.locus_id == locus.id).values(Bin.id)
                    locus_sample_annotation.alleles = dict([(str(id[0]), False) for id in bin_ids])
                    sample_annotation.locus_annotations.append(locus_sample_annotation)
            self.bulk_create_channel_annotations(channel_ids, block_commit=True)
            db.session.flush()
            channel_annotation_ids = [x.id for x in self.channel_annotations]
            print channel_annotation_ids
            # self.recalculate_channels(channel_annotation_ids, rescan_peaks=True, block_commit=True)
            db.session.commit()
            n += 1
        db.session.commit()

    def serialize(self):
        res = super(SampleBasedProject, self).serialize()
        res.update({
            'sample_annotations': []
        })
        return res

    def serialize_details(self):
        res = super(SampleBasedProject, self).serialize_details()
        sample_annotations = self.sample_annotations.all()
        res.update({
            'sample_annotations': [sample_annotation.serialize() for sample_annotation in sample_annotations]
        })
        return res

        # def locus_parameters(self):
        #     pass


class BinEstimatorProject(Project):
    # Collection of channels used to generate bins
    id = db.Column(db.Integer, db.ForeignKey('project.id'), primary_key=True)
    locus_bin_sets = db.relationship('LocusBinSet', lazy='immediate',
                                     cascade='save-update, merge, delete, delete-orphan')

    locus_parameters = db.relationship('BinEstimatorLocusParams', backref=db.backref('bin_estimator_project'),
                                       lazy='immediate',
                                       cascade='save-update, merge, delete, delete-orphan')

    __mapper_args__ = {'polymorphic_identity': 'bin_estimator_project'}

    def calculate_locus_bin_set(self, locus_id):
        self.delete_locus_bin_set(locus_id)
        locus_parameters = self.get_locus_parameters(locus_id)
        annotations = ProjectChannelAnnotations.query.join(Channel).filter(
            ProjectChannelAnnotations.project_id == self.id).filter(Channel.locus_id == locus_id).all()
        peaks = []
        for a in annotations:
            if a.annotated_peaks:
                peaks += a.annotated_peaks
        locus = Locus.query.get(locus_id)
        if locus not in self.locus_set.loci:
            raise ValueError("{} is not a member of this project's analysis set.".format(locus.label))
        if peaks:
            print peaks
            assert isinstance(locus_parameters, BinEstimatorLocusParams)
            locus_bin_set = LocusBinSet.from_peaks(locus_id=locus_id, peaks=peaks,
                                                   min_peak_frequency=locus_parameters.min_peak_frequency,
                                                   bin_buffer=locus_parameters.default_bin_buffer)
            self.locus_bin_sets.append(locus_bin_set)
        return self

    def calculate_locus_bin_sets(self):
        loci = self.locus_set.loci
        for locus in loci:
            self.calculate_locus_bin_set(locus.id)
        return self

    def delete_locus_bin_set(self, locus_id):
        old_sets = [x for x in self.locus_bin_sets if x.locus_id == locus_id]
        for s in old_sets:
            db.session.delete(s)

    def annotate_bins(self, peaks, locus_id):
        lbs = next(locus_bin_set for locus_bin_set in self.locus_bin_sets if locus_bin_set.locus_id == locus_id)
        assert isinstance(lbs, LocusBinSet)
        if peaks:
            peaks = lbs.peak_bin_annotator(peaks)
        return peaks

    def analyze_locus(self, locus_id, block_commit=False):
        super(BinEstimatorProject, self).analyze_locus(locus_id, block_commit)
        self.calculate_locus_bin_set(locus_id)
        projects = GenotypingProject.query.filter(GenotypingProject.bin_estimator_id == self.id).all()
        for project in projects:
            assert isinstance(project, GenotypingProject)
            project.bin_estimator_changed(locus_id)
        return self

    def initialize_project(self):
        loci = self.locus_set.loci
        for locus in loci:
            self.delete_locus_bin_set(locus.id)
        for ca in self.channel_annotations:
            db.session.delete(ca)
        for lp in self.locus_parameters:
            assert isinstance(lp, ProjectLocusParams)
            lp.scanning_parameters_stale = True
            lp.filter_parameters_stale = True
            channel_ids = set(Channel.query.filter(Channel.locus_id == lp.locus_id).values(Channel.id))
            self.bulk_create_channel_annotations(channel_ids)
        return self

    def serialize(self):
        res = super(BinEstimatorProject, self).serialize()
        res.update({
            'locus_bin_sets': {}
        })
        return res

    def serialize_details(self):
        res = super(BinEstimatorProject, self).serialize_details()
        res.update({
            'locus_bin_sets': {locus_bin_set.locus_id: locus_bin_set.serialize() for locus_bin_set in
                               self.locus_bin_sets}
        })
        return res


class LocusBinSet(BF.BinFinder, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    locus_id = db.Column(db.Integer, db.ForeignKey('locus.id', ondelete="CASCADE"))
    locus = db.relationship('Locus', lazy='immediate')
    project_id = db.Column(db.Integer, db.ForeignKey('bin_estimator_project.id', ondelete="CASCADE"))
    project = db.relationship('BinEstimatorProject')

    bins = db.relationship('Bin', lazy='immediate', cascade='save-update, merge, delete, delete-orphan')

    @classmethod
    def from_peaks(cls, locus_id, peaks, min_peak_frequency, bin_buffer):
        locus = Locus.query.get(locus_id)
        locus_bin_set = cls()
        locus_bin_set.locus = locus
        db.session.add(locus_bin_set)

        bin_set = BF.BinFinder()
        bin_set.calculate_bins(peaks=peaks, nucleotide_repeat_length=locus.nucleotide_repeat_length,
                               min_peak_frequency=min_peak_frequency, bin_buffer=bin_buffer)
        for b in bin_set.bins:
            assert isinstance(b, BF.Bin)
            bin = Bin(label=b.label, base_size=b.base_size, bin_buffer=b.bin_buffer, peak_count=b.peak_count)
            locus_bin_set.bins.append(bin)
        return locus_bin_set

    @reconstructor
    def init_on_load(self):
        super(LocusBinSet, self).__init__(self.bins)

    def serialize(self):
        res = {
            'id': self.id,
            'locus_id': self.locus_id,
            'project_id': self.project_id,
            'bins': {bin.id: bin.serialize() for bin in self.bins}
        }
        return res


class Bin(Flaggable, BF.Bin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    locus_bin_set_id = db.Column(db.Integer, db.ForeignKey('locus_bin_set.id', ondelete="CASCADE"))
    label = db.Column(db.Text, nullable=False)
    base_size = db.Column(db.Float, nullable=False)
    bin_buffer = db.Column(db.Float, nullable=False)
    peak_count = db.Column(db.Integer)

    def __repr__(self):
        return "<Bin {}>".format(self.label)

    @reconstructor
    def init_on_load(self):
        super(Bin, self).__init__(self.label, self.base_size, self.bin_buffer, self.peak_count)

    def serialize(self):
        res = {
            'locus_bin_set_id': self.locus_bin_set_id,
            'label': self.label,
            'base_size': self.base_size,
            'bin_buffer': self.bin_buffer,
            'peak_count': self.peak_count
        }
        return res


class ArtifactEstimatorProject(Project):
    id = db.Column(db.Integer, db.ForeignKey('project.id'), primary_key=True)
    bin_estimator_id = db.Column(db.Integer, db.ForeignKey('bin_estimator_project.id'), nullable=False)
    bin_estimator = db.relationship('BinEstimatorProject', foreign_keys=[bin_estimator_id])
    locus_artifact_estimators = db.relationship('LocusArtifactEstimator', lazy='immediate',
                                                cascade='save-update, merge, delete, delete-orphan')

    locus_parameters = db.relationship('ArtifactEstimatorLocusParams', lazy='immediate',
                                       backref=db.backref('artifact_estimator_project'),
                                       cascade='save-update, merge, delete, delete-orphan')

    __mapper_args__ = {'polymorphic_identity': 'artifact_estimator_project'}

    def add_channel(self, channel_id, block_commit=False):
        channel_annotation = super(ArtifactEstimatorProject, self).add_channel(channel_id, block_commit)
        assert isinstance(self.bin_estimator, BinEstimatorProject)
        self.bin_estimator.annotate_bins(channel_annotation.annotated_peaks, channel_annotation.channel.locus_id)
        return channel_annotation

    def add_channels(self, channel_ids, block_commit=False):
        channel_annotations = []

        for channel_id in channel_ids:
            channel_annotation = self.add_channel(channel_id, block_commit=True)
            channel_annotations.append(channel_annotation)

        if not block_commit:
            db.session.commit()

        return channel_annotations

    def annotate_channel(self, channel_annotation):
        if channel_annotation.annotated_peaks:
            if self.bin_estimator:
                channel_annotation.annotated_peaks = self.bin_estimator.annotate_bins(
                    channel_annotation.annotated_peaks,
                    channel_annotation.channel.locus_id)

    def recalculate_channel(self, channel_annotation_id, rescan_peaks, block_commit=False):
        channel_annotation = super(ArtifactEstimatorProject, self).recalculate_channel(channel_annotation_id,
                                                                                       rescan_peaks,
                                                                                       block_commit=True)
        self.annotate_channel(channel_annotation)

        if not block_commit:
            db.session.commit()

        return channel_annotation

    def recalculate_channels(self, channel_annotation_ids, rescan_peaks, block_commit=False):
        channel_annotations = super(ArtifactEstimatorProject, self).recalculate_channels(channel_annotation_ids,
                                                                                         rescan_peaks,
                                                                                         block_commit=True)
        for channel_annotation in channel_annotations:
            self.annotate_channel(channel_annotation)

        if not block_commit:
            db.session.commit()

        return channel_annotations

    def delete_locus_artifact_estimator(self, locus_id):
        old_estimators = [x for x in self.locus_artifact_estimators if x.locus_id == locus_id]
        print old_estimators
        for e in old_estimators:
            db.session.delete(e)
        db.session.commit()
        return ArtifactEstimatorProject.query.get(self.id)

    def calculate_locus_artifact_estimator(self, locus_id):
        self.delete_locus_artifact_estimator(locus_id)
        annotations = []
        channel_annotations = self.get_locus_channel_annotations(locus_id)
        locus_parameters = self.get_locus_parameters(locus_id)
        assert isinstance(locus_parameters, ArtifactEstimatorLocusParams)
        max_relative_peak_height = locus_parameters.max_secondary_relative_peak_height
        print max_relative_peak_height
        for channel_annotation in channel_annotations:
            peaks = channel_annotation.annotated_peaks
            if peaks:
                main_peaks = []
                secondary_peaks = []
                for peak in peaks:
                    if peak['relative_peak_height'] < max_relative_peak_height:
                        secondary_peaks.append(peak)
                    else:
                        main_peaks.append(peak)
                if len(main_peaks) == 1 and main_peaks[0]['relative_peak_height'] == 1:
                    if secondary_peaks:
                        annotations.append(peaks)
        locus_artifact_estimator = None
        print self.locus_artifact_estimators

        if annotations:
            print "Estimating Artifact"
            locus_artifact_estimator = LocusArtifactEstimator.from_peaks(locus_id, annotations,
                                                                         locus_parameters.min_artifact_peak_frequency)
            db.session.add(locus_artifact_estimator)
            self.locus_artifact_estimators.append(locus_artifact_estimator)

        return locus_artifact_estimator

    def calculate_locus_artifact_estimators(self):
        loci = self.locus_set.loci
        for locus in loci:
            self.calculate_locus_artifact_estimator(locus.id)
        return self

    def annotate_artifact(self, annotated_peaks, locus_id):
        if annotated_peaks:
            for peak in annotated_peaks:
                peak['artifact_contribution'] = 0
                peak['artifact_error'] = 0
            artifact_annotator = next(
                locus_artifact_estimator for locus_artifact_estimator in self.locus_artifact_estimators if
                locus_artifact_estimator.locus_id == locus_id)
            assert isinstance(artifact_annotator, LocusArtifactEstimator)
            annotated_peaks = artifact_annotator.annotate_artifact(annotated_peaks)
        return annotated_peaks

    def analyze_locus(self, locus_id, block_commit=False):
        super(ArtifactEstimatorProject, self).analyze_locus(locus_id, block_commit)
        self.calculate_locus_artifact_estimator(locus_id)
        projects = GenotypingProject.query.filter(GenotypingProject.artifact_estimator_id == self.id).all()
        for project in projects:
            assert isinstance(project, GenotypingProject)
            project.artifact_estimator_changed(locus_id)
        return self

    def initialize_project(self):
        loci = self.locus_set.loci
        for locus in loci:
            self.delete_locus_artifact_estimator(locus.id)
        for ca in self.channel_annotations:
            db.session.delete(ca)
        for lp in self.locus_parameters:
            assert isinstance(lp, ArtifactEstimatorLocusParams)
            lp.scanning_parameters_stale = True
            lp.filter_parameters_stale = True
            channel_ids = set(Channel.query.filter(Channel.locus_id == lp.locus_id).values(Channel.id))
            self.bulk_create_channel_annotations(channel_ids)
        return self

    def serialize(self):
        res = super(ArtifactEstimatorProject, self).serialize()
        res.update({
            'bin_estimator_id': self.bin_estimator_id,
            'locus_artifact_estimators': {}
        })
        return res

    def serialize_details(self):
        res = super(ArtifactEstimatorProject, self).serialize_details()
        res.update({
            'bin_estimator_id': self.bin_estimator_id,
            'locus_artifact_estimators': {locus_artifact_estimator.locus_id: locus_artifact_estimator.serialize() for
                                          locus_artifact_estimator in self.locus_artifact_estimators}
        })
        return res



class LocusArtifactEstimator(AE.ArtifactEstimatorSet, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    locus_id = db.Column(db.Integer, db.ForeignKey('locus.id', ondelete="CASCADE"))
    locus = db.relationship('Locus')
    project_id = db.Column(db.Integer, db.ForeignKey('artifact_estimator_project.id', ondelete="CASCADE"))
    project = db.relationship('ArtifactEstimatorProject')
    artifact_estimators = db.relationship('ArtifactEstimator', lazy='immediate',
                                          cascade='save-update, merge, delete, delete-orphan')

    def __repr__(self):
        return "<Artifact Estimator {}>".format(self.locus.label)

    @classmethod
    def from_peaks(cls, locus_id, peak_sets, min_artifact_peak_frequency):
        locus = Locus.query.get(locus_id)
        locus_artifact_estimator = cls()
        locus_artifact_estimator.locus = locus

        db.session.add(locus_artifact_estimator)

        ae = AE.ArtifactEstimatorSet.from_peaks(peak_sets=peak_sets, start_size=locus.min_base_length,
                                                end_size=locus.max_base_length,
                                                min_artifact_peak_frequency=min_artifact_peak_frequency,
                                                nucleotide_repeat_length=locus.nucleotide_repeat_length)

        for estimator in ae.artifact_estimators:
            assert isinstance(estimator, AE.ArtifactEstimator)
            artifact_estimator = ArtifactEstimator(artifact_distance=estimator.artifact_distance,
                                                   artifact_distance_buffer=estimator.artifact_distance_buffer,
                                                   peak_data=estimator.peak_data)
            for eqn in estimator.artifact_equations:
                assert isinstance(eqn, AE.ArtifactEquation)
                artifact_equation = ArtifactEquation(sd=eqn.sd, r_squared=eqn.r_squared, slope=eqn.slope,
                                                     intercept=eqn.intercept, start_size=eqn.start_size,
                                                     end_size=eqn.end_size)
                artifact_estimator.artifact_equations.append(artifact_equation)
            locus_artifact_estimator.artifact_estimators.append(artifact_estimator)

        return locus_artifact_estimator

    @reconstructor
    def init_on_load(self):
        super(LocusArtifactEstimator, self).__init__(self.artifact_estimators)

    def serialize(self):
        res = {
            'id': self.id,
            'locus_id': self.locus_id,
            'project_id': self.project_id,
            'artifact_estimators': [artifact_estimator.serialize() for artifact_estimator in self.artifact_estimators]
        }
        return res


class ArtifactEstimator(AE.ArtifactEstimator, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    artifact_distance = db.Column(db.Float, nullable=False)
    artifact_distance_buffer = db.Column(db.Float, nullable=False)
    locus_artifact_estimator_id = db.Column(db.Integer,
                                            db.ForeignKey('locus_artifact_estimator.id', ondelete="CASCADE"))
    locus_artifact_estimator = db.relationship('LocusArtifactEstimator')
    artifact_equations = db.relationship('ArtifactEquation', lazy='immediate',
                                         cascade='save-update, merge, delete, delete-orphan')
    peak_data = db.Column(MutableList.as_mutable(JSONEncodedData))

    @reconstructor
    def init_on_load(self):
        super(ArtifactEstimator, self).__init__(self.artifact_distance, self.artifact_distance_buffer, self.peak_data,
                                                self.artifact_equations)

    def generate_estimating_equations(self, parameter_sets):
        for eq in self.artifact_equations:
            db.session.delete(eq)
        self.artifact_equations = []
        artifact_equations = super(ArtifactEstimator, self).generate_estimating_equations(parameter_sets)
        print artifact_equations
        for ae in artifact_equations:
            self.artifact_equations.append(
                ArtifactEquation(sd=ae.sd, r_squared=ae.r_squared, slope=ae.slope, intercept=ae.intercept,
                                 start_size=ae.start_size, end_size=ae.end_size))
        return self

    def add_breakpoint(self, breakpoint):
        """
        :type breakpoint: float
        """
        old_param_sets = [{
                              'start_size': eq.start_size,
                              'end_size': eq.end_size,
                              'method': 'TSR'
                          } for eq in self.artifact_equations]

        param_sets = []
        for param_set in old_param_sets:
            if param_set['start_size'] < breakpoint < param_set['end_size']:
                param_sets.append({
                    'start_size': param_set['start_size'],
                    'end_size': breakpoint,
                    'method': 'TSR'
                })
                param_sets.append({
                    'start_size': breakpoint,
                    'end_size': param_set['end_size'],
                    'method': 'TSR'
                })
            else:
                param_sets.append(param_set)
        self.generate_estimating_equations(param_sets)
        return self

    def clear_breakpoints(self):
        param_sets = [{
            'start_size': self.locus_artifact_estimator.locus.min_base_length,
            'end_size': self.locus_artifact_estimator.locus.max_base_length,
            'method': 'TSR'
        }]
        self.generate_estimating_equations(param_sets)
        return self

    def serialize(self):
        res = {
            'id': self.id,
            'artifact_distance': self.artifact_distance,
            'artifact_distance_buffer': self.artifact_distance_buffer,
            'locus_artifact_estimator_id': self.locus_artifact_estimator_id,
            'peak_data': self.peak_data,
            'artifact_equations': [eqn.serialize() for eqn in self.artifact_equations]
        }
        return res


class ArtifactEquation(Flaggable, AE.ArtifactEquation, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    artifact_estimator_id = db.Column(db.Integer, db.ForeignKey('artifact_estimator.id', ondelete="CASCADE"))
    sd = db.Column(db.Float, nullable=False)
    r_squared = db.Column(db.Float, nullable=True)
    slope = db.Column(db.Float, nullable=False)
    intercept = db.Column(db.Float, nullable=False)
    start_size = db.Column(db.Float, nullable=False)
    end_size = db.Column(db.Float, nullable=False)

    def __repr__(self):
        return "<Artifact Equation y = {}x + {}".format(self.slope, self.intercept)

    @reconstructor
    def init_on_load(self):
        super(ArtifactEquation, self).__init__(self.sd, self.r_squared, self.slope, self.intercept, self.start_size,
                                               self.end_size)

    def serialize(self):
        res = {
            'id': self.id,
            'artifact_estimator_id': self.artifact_estimator_id,
            'sd': self.sd,
            'r_squared': self.r_squared,
            'slope': self.slope,
            'intercept': self.intercept,
            'start_size': self.start_size,
            'end_size': self.end_size
        }
        return res


class GenotypingProject(SampleBasedProject, BinEstimating, ArtifactEstimating):
    # Collection of methods to annotate peaks with artifact, bin in which a peak falls, probabilistic estimate of peak
    id = db.Column(db.Integer, db.ForeignKey('project.id'), primary_key=True)
    locus_parameters = db.relationship('GenotypingLocusParams', backref=db.backref('genotyping_project'), lazy='select',
                                       cascade='save-update, merge, delete, delete-orphan')

    probability_threshold = db.Column(db.Float, default=.5, nullable=False)

    __mapper_args__ = {'polymorphic_identity': 'genotyping_project'}

    def __init__(self, locus_set_id, bin_estimator_id, artifact_estimator_id, **kwargs):
        super(GenotypingProject, self).__init__(locus_set_id, **kwargs)
        self.bin_estimator_id = bin_estimator_id
        self.artifact_estimator_id = artifact_estimator_id

    def clear_sample_annotations(self, locus_id):
        sample_locus_annotations = SampleLocusAnnotation.query.join(ProjectSampleAnnotations).filter(
            SampleLocusAnnotation.locus_id == locus_id).filter(ProjectSampleAnnotations.project_id == self.id).all()
        for sample_annotation in sample_locus_annotations:
            assert isinstance(sample_annotation, SampleLocusAnnotation)
            sample_annotation.annotated_peaks = []
            sample_annotation.reference_run_id = None
            sample_annotation.flags = {}

    def clear_bin_annotations(self, locus_id):
        channel_annotations = ProjectChannelAnnotations.query.join(Channel).filter(Channel.locus_id == locus_id).filter(
            ProjectChannelAnnotations.project_id == self.id).all()
        for annotation in channel_annotations:
            assert isinstance(annotation, ProjectChannelAnnotations)
            if annotation.annotated_peaks:
                for peak in annotation.annotated_peaks:
                    peak['in_bin'] = False
                    peak['bin'] = ""
                    peak['bin_id'] = None

        self.clear_sample_annotations(locus_id)
        return self

    def clear_artifact_annotations(self, locus_id):
        channel_annotations = ProjectChannelAnnotations.query.join(Channel).filter(Channel.locus_id == locus_id).filter(ProjectChannelAnnotations.project_id == self.id).all()
        for annotation in channel_annotations:
            assert isinstance(annotation, ProjectChannelAnnotations)
            if annotation.annotated_peaks:
                for peak in annotation.annotated_peaks:
                    peak['artifact_contribution'] = 0
                    peak['artifact_error'] = 0
        self.clear_sample_annotations(locus_id)
        return self

    def bin_estimator_changed(self, locus_id):
        self.clear_bin_annotations(locus_id)
        self.initialize_alleles(locus_id)
        lp = self.get_locus_parameters(locus_id)
        lp.filter_parameters_stale = True
        return self

    def artifact_estimator_changed(self, locus_id):
        self.clear_artifact_annotations(locus_id)
        self.initialize_alleles(locus_id)
        lp = self.get_locus_parameters(locus_id)
        lp.filter_parameters_stale = True
        return self

    def annotate_channel(self, channel_annotation):
        assert isinstance(channel_annotation, ProjectChannelAnnotations)
        print "Annotating Channel " + str(self)
        if channel_annotation.annotated_peaks:
            if self.bin_estimator:
                for peak in channel_annotation.annotated_peaks:
                    peak['in_bin'] = False
                    peak['bin'] = ""
                    peak['bin_id'] = None
                print 'Annotating Bins'
                channel_annotation.annotated_peaks = self.bin_estimator.annotate_bins(
                    channel_annotation.annotated_peaks,
                    channel_annotation.channel.locus_id)
            if self.artifact_estimator:
                for peak in channel_annotation.annotated_peaks:
                    peak['artifact_contribution'] = 0
                    peak['artifact_error'] = 0
                print 'Annotating Artifact'
                channel_annotation.annotated_peaks = self.artifact_estimator.annotate_artifact(
                    channel_annotation.annotated_peaks, channel_annotation.channel.locus_id)
            channel_annotation.annotated_peaks.changed()

    def recalculate_channel(self, channel_annotation_id, rescan_peaks, block_commit=False):
        print "Recalculating Channel " + str(self)
        channel_annotation = super(GenotypingProject, self).recalculate_channel(channel_annotation_id, rescan_peaks,
                                                                                block_commit=True)
        self.annotate_channel(channel_annotation)

        if not block_commit:
            db.session.commit()

        return channel_annotation

    def recalculate_channels(self, channel_annotation_ids, rescan_peaks, block_commit=False):
        channel_annotations = super(GenotypingProject, self).recalculate_channels(channel_annotation_ids, rescan_peaks,
                                                                                  block_commit=True)

        for channel_annotation in channel_annotations:
            self.annotate_channel(channel_annotation)

        if not block_commit:
            db.session.commit()

        return channel_annotations

    def add_channel(self, channel_id, block_commit=False):
        channel_annotation = ProjectChannelAnnotations.query.filter(
            ProjectChannelAnnotations.channel_id == channel_id).filter(
            ProjectChannelAnnotations.project_id == self.id).first()
        if not channel_annotation:
            print "Adding new channel"
            channel_annotation = super(GenotypingProject, self).add_channel(channel_id, block_commit=True)
            # self.annotate_channel(channel_annotation)
            if not block_commit:
                db.session.commit()
        else:
            print "Channel Already Added to project."
        return channel_annotation

    def add_channels(self, channel_ids, block_commit=False):
        channel_annotations = super(GenotypingProject, self).add_channels(channel_ids, block_commit=True)

        # for channel_annotation in channel_annotations:
        #     self.annotate_channel(channel_annotation)

        if not block_commit:
            db.session.commit()

        return channel_annotations

    def add_sample(self, sample_id, block_commit=False):
        sample_annotation = super(GenotypingProject, self).add_sample(sample_id, block_commit=block_commit)
        for locus in self.locus_set.loci:
            locus_sample_annotation = SampleLocusAnnotation(locus_id=locus.id)
            bin_ids = Bin.query.join(LocusBinSet).join(BinEstimatorProject).filter(
                BinEstimatorProject.id == self.bin_estimator_id).filter(LocusBinSet.locus_id == locus.id).values(Bin.id)
            locus_sample_annotation.alleles = dict([(str(id[0]), False) for id in bin_ids])
            sample_annotation.locus_annotations.append(locus_sample_annotation)

    def annotate_peak_probability(self):
        pass

    def analyze_locus(self, locus_id, block_commit=False):
        super(GenotypingProject, self).analyze_locus(locus_id, block_commit)
        self.analyze_samples(locus_id)
        return self

    def analyze_samples(self, locus_id):
        locus_params = self.get_locus_parameters(locus_id)
        assert isinstance(locus_params, GenotypingLocusParams)
        locus_annotations = SampleLocusAnnotation.query.join(ProjectSampleAnnotations).filter(
            ProjectSampleAnnotations.project_id == self.id).filter(SampleLocusAnnotation.locus_id == locus_id).all()
        for locus_annotation in locus_annotations:
            print locus_annotation
            assert isinstance(locus_annotation, SampleLocusAnnotation)
            channel_annotation = self.get_best_run(locus_annotation.sample_annotation.sample_id,
                                                   locus_annotation.locus_id)
            if channel_annotation:
                locus_annotation.reference_run = channel_annotation
                peaks = channel_annotation.annotated_peaks[:]

                for peak in peaks:
                    peak['flags'] = {
                        'below_relative_threshold': False,
                        'bleedthrough': False,
                        'crosstalk': False,
                        'artifact': False
                    }

                    if peak['relative_peak_height'] < locus_params.relative_peak_height_limit:
                        peak['flags']['below_relative_threshold'] = True

                    adjusted_peak_height = peak['peak_height'] - peak['artifact_contribution'] - (
                        peak['artifact_error'] * locus_params.hard_artifact_sd_limit)

                    if adjusted_peak_height < locus_params.absolute_peak_height_limit:
                        peak['flags']['artifact'] = True

                    if peak['bleedthrough_ratio'] > locus_params.bleedthrough_filter_limit or peak['peak_height'] * \
                            peak['bleedthrough_ratio'] > locus_params.offscale_threshold:
                        peak['flags']['bleedthrough'] = True

                    if peak['crosstalk_ratio'] > locus_params.crosstalk_filter_limit or peak['peak_height'] * \
                            peak['crosstalk_ratio'] > locus_params.offscale_threshold:
                        peak['flags']['crosstalk'] = True

                locus_annotation.annotated_peaks = peaks

                if any([x['peak_height'] > locus_params.failure_threshold for x in locus_annotation.annotated_peaks]):
                    locus_annotation.flags['failure'] = False
                else:
                    locus_annotation.flags['failure'] = True

                if any([(x['peak_height'] > locus_params.offscale_threshold) or
                                                x['peak_height'] * x[
                                            'bleedthrough_ratio'] > locus_params.offscale_threshold or
                                                x['peak_height'] * x[
                                            'crosstalk_ratio'] > locus_params.offscale_threshold
                        for x in locus_annotation.annotated_peaks]):
                    locus_annotation.flags['offscale'] = True
                else:
                    locus_annotation.flags['offscale'] = False

                locus_annotation.flags['manual_curation'] = False

                locus_annotation.alleles = dict.fromkeys(locus_annotation.alleles, False)

                if not locus_annotation.flags['failure']:
                    for peak in locus_annotation.annotated_peaks:
                        if peak.get('in_bin', False) and not any(peak['flags'].values()):
                            locus_annotation.alleles[str(peak['bin_id'])] = True
            else:
                locus_annotation.reference_run = None
                locus_annotation.annotated_peaks = []
        return self

    def probabilistic_peak_annotation(self):
        # generate allele frequencies
        # for each sample, find MOI using "real peaks" only => real peaks are greater than hard artifact threshold

        all_locus_annotations = SampleLocusAnnotation.query.join(ProjectSampleAnnotations).filter(
            ProjectSampleAnnotations.project_id == self.id).all()

        locus_annotation_dict = defaultdict(list)

        for annotation in all_locus_annotations:
            assert isinstance(annotation, SampleLocusAnnotation)
            locus_annotation_dict[annotation.sample_annotations_id].append(annotation)

        locus_param_cache = {}

        print "Initializing Probabilities"

        for locus_annotation in all_locus_annotations:
            if locus_annotation.annotated_peaks and not locus_annotation.flags['failure']:
                for peak in locus_annotation.annotated_peaks:
                    if peak.get('in_bin') and not any(peak['flags'].values()):
                        peak['probability'] = 1
                    else:
                        peak['probability'] = 0
                locus_annotation.annotated_peaks.changed()

        db.session.flush()

        sample_annotations = self.sample_annotations.all()

        alleles_changed = True
        cycles = 0
        while alleles_changed:
            cycles += 1
            alleles_changed = False

            allele_counts = defaultdict(lambda: defaultdict(int))
            locus_totals = defaultdict(int)

            for locus_annotation in all_locus_annotations:
                assert isinstance(locus_annotation, SampleLocusAnnotation)
                if locus_annotation.annotated_peaks and not locus_annotation.flags['failure']:
                    locus_totals[locus_annotation.locus_id] += 1
                    for peak in locus_annotation.annotated_peaks:
                        if peak['in_bin'] and not any(peak['flags'].values()) and peak['probability'] >= self.probability_threshold:
                            allele_counts[locus_annotation.locus_id][peak['bin_id']] += 1

            allele_frequencies = defaultdict(dict)

            for locus in allele_counts.keys():
                for allele in allele_counts[locus].keys():
                    allele_frequencies[locus][allele] = allele_counts[locus][allele] / float(locus_totals[locus])

            for sample_annotation in sample_annotations:
                print sample_annotation
                assert isinstance(sample_annotation, ProjectSampleAnnotations)
                sample_annotation.moi = 0
                for locus_annotation in locus_annotation_dict[sample_annotation.id]:
                    if locus_annotation.annotated_peaks and not locus_annotation.flags['failure']:
                        sample_annotation.moi = max(sample_annotation.moi, len(
                            [x for x in locus_annotation.annotated_peaks if
                             x['probability'] >= self.probability_threshold]))

                for locus_annotation in locus_annotation_dict[sample_annotation.id]:
                    if locus_annotation.annotated_peaks and not locus_annotation.flags['failure']:
                        if not locus_param_cache.get(locus_annotation.locus_id, None):
                            locus_param_cache[locus_annotation.locus_id] = self.get_locus_parameters(
                                locus_annotation.locus_id)

                        locus_params = locus_param_cache.get(locus_annotation.locus_id)
                        assert isinstance(locus_params, GenotypingLocusParams)

                        peaks_copy = locus_annotation.annotated_peaks[:]
                        all_peaks = [x for x in peaks_copy if x['probability'] > self.probability_threshold]
                        possible_artifact_peaks = [x for x in all_peaks if (
                        x['peak_height'] - x['artifact_contribution'] - (x[
                                                                             'artifact_error'] * locus_params.soft_artifact_sd_limit)) <= locus_params.absolute_peak_height_limit]
                        new_probs = {}
                        for peak in possible_artifact_peaks:
                            other_peaks = [x for x in all_peaks if x['peak_index'] != peak['peak_index']]
                            this_peak_freq = allele_frequencies[locus_annotation.locus_id][peak['bin_id']] * peak[
                                'probability']
                            other_peak_freqs = [
                                allele_frequencies[locus_annotation.locus_id][x['bin_id']] * x['probability'] for x in
                                other_peaks]
                            total_probability = (sum(other_peak_freqs) + this_peak_freq) ** sample_annotation.moi
                            new_probs[peak['peak_index']] = (total_probability - (
                            sum(other_peak_freqs) ** sample_annotation.moi)) / total_probability

                        for peak in possible_artifact_peaks:
                            print "Old Probability:" + str(peak['probability'])
                            print "New Probability:" + str(new_probs[peak['peak_index']])
                            if new_probs[peak['peak_index']] < self.probability_threshold:
                                alleles_changed = True
                            peak['probability'] = new_probs[peak['peak_index']]
                        locus_annotation.annotated_peaks = peaks_copy

        for locus_annotation in all_locus_annotations:
            locus_annotation.annotated_peaks.changed()

        for sample_annotation in sample_annotations:
            for locus_annotation in locus_annotation_dict[sample_annotation.id]:
                locus_annotation.alleles = dict.fromkeys(locus_annotation.alleles, False)
                locus_annotation.alleles.changed()
                if locus_annotation.annotated_peaks and not locus_annotation.flags['failure']:
                    for peak in locus_annotation.annotated_peaks:
                        if peak['probability'] >= self.probability_threshold:
                            locus_annotation.alleles[str(peak['bin_id'])] = True
                            locus_annotation.alleles.changed()

        db.session.flush()
        print "Cycles Completed: " + str(cycles)
        return self

    def serialize(self):
        res = super(GenotypingProject, self).serialize()
        res.update({
            'bin_estimator_id': self.bin_estimator_id,
            'artifact_estimator_id': self.artifact_estimator_id,
        })
        return res

    def serialize_details(self):
        res = super(GenotypingProject, self).serialize_details()
        res.update({
            'bin_estimator_id': self.bin_estimator_id,
            'artifact_estimator_id': self.artifact_estimator_id,
            'sample_annotations': {x.id: x.serialize() for x in self.sample_annotations}
        })
        return res

    def get_best_run(self, sample_id, locus_id):
        locus_params = self.get_locus_parameters(locus_id)
        assert isinstance(locus_params, GenotypingLocusParams)
        channel_annotations = ProjectChannelAnnotations.query.join(Channel).filter(
            ProjectChannelAnnotations.project_id == self.id).filter(
            Channel.sample_id == sample_id).filter(Channel.locus_id == locus_id).all()

        channel_annotations = [x for x in channel_annotations if
                               x.channel.well.sizing_quality < x.channel.well.ladder.unusable_sq_limit]

        best_annotation = None
        for annotation in channel_annotations:
            if not annotation.annotated_peaks:
                annotation.annotated_peaks = []
            assert isinstance(annotation, ProjectChannelAnnotations)
            if not best_annotation:
                best_annotation = annotation
            else:
                best_peaks = filter(lambda y: y['peak_height'] < locus_params.offscale_threshold and y['in_bin'],
                                    best_annotation.annotated_peaks)

                if best_peaks:
                    max_best_peak = max(best_peaks, key=lambda x: x['peak_height'])
                else:
                    max_best_peak = {'peak_height': 0}

                curr_peaks = filter(lambda y: y['peak_height'] < locus_params.offscale_threshold and y['in_bin'],
                                    annotation.annotated_peaks)

                if curr_peaks:
                    max_curr_peak = max(curr_peaks, key=lambda x: x['peak_height'])
                else:
                    max_curr_peak = {'peak_height': 0}

                if max_curr_peak['peak_height'] > max_best_peak['peak_height']:
                    best_annotation = annotation
        return best_annotation

    def initialize_alleles(self, locus_id):
        locus_sample_annotations = SampleLocusAnnotation.query.join(ProjectSampleAnnotations).filter(
            ProjectSampleAnnotations.project_id == self.id).filter(SampleLocusAnnotation.locus_id == locus_id).all()

        q = Bin.query.join(LocusBinSet).join(BinEstimatorProject).filter(
            BinEstimatorProject.id == self.bin_estimator_id).filter(
            LocusBinSet.locus_id == locus_id)

        print self.bin_estimator_id
        print locus_id

        bin_ids = Bin.query.join(LocusBinSet).join(BinEstimatorProject).filter(
            BinEstimatorProject.id == self.bin_estimator_id).filter(
            LocusBinSet.locus_id == locus_id).values(Bin.id)

        bin_ids = list(bin_ids)

        for annotation in locus_sample_annotations:
            assert isinstance(annotation, SampleLocusAnnotation)
            annotation.alleles = {}
            for id in bin_ids:
                annotation.alleles[str(id[0])] = False
        return self


class ProjectLocusParams(PeakScanner, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    locus_id = db.Column(db.Integer, db.ForeignKey("locus.id", ondelete="CASCADE"))
    project_id = db.Column(db.Integer, db.ForeignKey("project.id", ondelete="CASCADE"))
    locus = db.relationship('Locus', lazy='immediate')

    # Peak Filter Params
    min_peak_height = db.Column(db.Integer, default=150, nullable=False)
    max_peak_height = db.Column(db.Integer, default=40000, nullable=False)
    min_peak_height_ratio = db.Column(db.Float, default=0, nullable=False)
    max_bleedthrough = db.Column(db.Float, default=4, nullable=False)
    max_crosstalk = db.Column(db.Float, default=4, nullable=False)
    min_peak_distance = db.Column(db.Float, default=2, nullable=False)

    scanning_parameters_stale = db.Column(db.Boolean, default=True, nullable=False)
    filter_parameters_stale = db.Column(db.Boolean, default=True, nullable=False)

    discriminator = db.Column('type', db.String(255))
    __mapper_args__ = {'polymorphic_on': discriminator,
                       'polymorphic_identity': 'base_locus_params'}

    @property
    def filter_parameters(self):
        return {
            'min_peak_height': self.min_peak_height,
            'max_peak_height': self.max_peak_height,
            'min_peak_height_ratio': self.min_peak_height_ratio,
            'max_bleedthrough': self.max_bleedthrough,
            'max_crosstalk': self.max_crosstalk,
            'min_peak_distance': self.min_peak_distance
        }

    @staticmethod
    def stale_parameters(mapper, connection, target):
        print "Checking for Stale Paramaters"
        assert isinstance(target, ProjectLocusParams)

        filter_params = target.filter_parameters.keys()
        scanning_params = target.scanning_parameters.keys()

        if params_changed(target, filter_params):
            print "Filter Parameters Stale"
            print target
            print target.filter_parameters_stale
            target.filter_parameters_stale = True
            print target.filter_parameters_stale

        if params_changed(target, scanning_params):
            print "Scanning Parameters Stale"
            target.scanning_parameters_stale = True

    @classmethod
    def __declare_last__(cls):
        event.listen(cls, 'before_update', cls.stale_parameters)

    def serialize(self):
        res = {
            'id': self.id,
            'locus_id': self.locus_id,
            'project_id': self.project_id,
            'filter_parameters_stale': self.filter_parameters_stale,
            'scanning_parameters_stale': self.scanning_parameters_stale
        }
        res.update(self.scanning_parameters)
        res.update(self.filter_parameters)
        return res

    def __repr__(self):
        return "<{} {} {}>".format(self.__class__.__name__, self.locus.label, self.locus.color)


class ArtifactEstimatorLocusParams(ProjectLocusParams):
    id = db.Column(db.Integer, db.ForeignKey('project_locus_params.id'), primary_key=True)
    max_secondary_relative_peak_height = db.Column(db.Float, default=.4, nullable=False)
    min_artifact_peak_frequency = db.Column(db.Integer, default=10, nullable=False)

    __mapper_args__ = {
        'polymorphic_identity': 'artifact_estimator_locus_params',
    }

    def serialize(self):
        res = super(ArtifactEstimatorLocusParams, self).serialize()
        res.update({
            'max_secondary_relative_peak_height': self.max_secondary_relative_peak_height,
            'min_artifact_peak_frequency': self.min_artifact_peak_frequency
        })
        return res

    @classmethod
    def __declare_last__(cls):
        event.listen(cls, 'before_update', cls.stale_parameters)


class GenotypingLocusParams(ProjectLocusParams):
    id = db.Column(db.Integer, db.ForeignKey('project_locus_params.id'), primary_key=True)
    soft_artifact_sd_limit = db.Column(db.Float, default=3)
    hard_artifact_sd_limit = db.Column(db.Float, default=1)
    offscale_threshold = db.Column(db.Integer, default=32000, nullable=False)
    bleedthrough_filter_limit = db.Column(db.Float, default=2, nullable=False)
    crosstalk_filter_limit = db.Column(db.Float, default=2, nullable=False)
    relative_peak_height_limit = db.Column(db.Float, default=0.01, nullable=False)
    absolute_peak_height_limit = db.Column(db.Integer, default=50, nullable=False)
    failure_threshold = db.Column(db.Integer, default=500, nullable=False)

    __mapper_args__ = {
        'polymorphic_identity': 'genotyping_locus_params',
    }

    def serialize(self):
        res = super(GenotypingLocusParams, self).serialize()
        res.update({
            'soft_artifact_sd_limit': self.soft_artifact_sd_limit,
            'hard_artifact_sd_limit': self.hard_artifact_sd_limit,
            'offscale_threshold': self.offscale_threshold,
            'bleedthrough_filter_limit': self.bleedthrough_filter_limit,
            'crosstalk_filter_limit': self.crosstalk_filter_limit,
            'relative_peak_height_limit': self.relative_peak_height_limit,
            'absolute_peak_height_limit': self.absolute_peak_height_limit,
            'failure_threshold': self.failure_threshold
        })
        return res

    @classmethod
    def __declare_last__(cls):
        event.listen(cls, 'before_update', cls.stale_parameters)


class BinEstimatorLocusParams(ProjectLocusParams):
    id = db.Column(db.Integer, db.ForeignKey('project_locus_params.id'), primary_key=True)
    min_peak_frequency = db.Column(db.Integer, default=10, nullable=False)
    default_bin_buffer = db.Column(db.Float, default=.75, nullable=False)

    __mapper_args__ = {
        'polymorphic_identity': 'bin_estimator_locus_params'
    }

    def serialize(self):
        res = super(BinEstimatorLocusParams, self).serialize()
        res.update({
            'min_peak_frequency': self.min_peak_frequency,
            'default_bin_buffer': self.default_bin_buffer
        })
        return res

    @classmethod
    def __declare_last__(cls):
        event.listen(cls, 'before_update', cls.stale_parameters)


class ProjectChannelAnnotations(TimeStamped, db.Model):
    """
    Channel level analysis in a project.
    """
    id = db.Column(db.Integer, primary_key=True)
    channel_id = db.Column(db.Integer, db.ForeignKey("channel.id", ondelete="CASCADE"), index=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id", ondelete="CASCADE"), index=True)
    channel = db.relationship('Channel', lazy='select')
    annotated_peaks = db.Column(MutableList.as_mutable(JSONEncodedData), default=[])
    peak_indices = db.Column(MutableList.as_mutable(JSONEncodedData))
    __table_args__ = (db.UniqueConstraint('project_id', 'channel_id', name='_project_channel_uc'),)

    def serialize(self):
        res = {
            'id': self.id,
            'channel_id': self.channel_id,
            'project_id': self.project_id,
            'annotated_peaks': self.annotated_peaks or [],
            'last_updated': self.last_updated
        }
        return res


class ProjectSampleAnnotations(TimeStamped, db.Model):
    """
    Sample level analysis in a project.
    """
    id = db.Column(db.Integer, primary_key=True)
    sample_id = db.Column(db.Integer, db.ForeignKey('sample.id', ondelete="CASCADE"), index=True)
    project_id = db.Column(db.Integer, db.ForeignKey('project.id', ondelete="CASCADE"), index=True)
    locus_annotations = db.relationship('SampleLocusAnnotation', backref=db.backref('sample_annotation'),
                                        lazy='dynamic',
                                        cascade='save-update, merge, delete, delete-orphan')
    sample = db.relationship('Sample', lazy='immediate')
    moi = db.Column(db.Integer)
    __table_args__ = (db.UniqueConstraint('project_id', 'sample_id', name='_project_sample_uc'),)

    def get_best_run(self, locus_id):
        """
        Return the best set of annotations for a sample at a given locus
        :type locus_id: int
        :rtype: ProjectChannelAnnotations
        """
        locus_parameters = self.project.get_locus_parameters(locus_id)
        channel_annotations = ProjectChannelAnnotations.query.join(Channel).join(Well).filter(
            ProjectChannelAnnotations.project_id == self.project_id).filter(
            Channel.sample_id == self.sample_id).filter(Well.sizing_quality < 15).all()

        best_annotation = None
        for annotation in channel_annotations:
            if not best_annotation:
                assert isinstance(annotation, ProjectChannelAnnotations)
                best_annotation = annotation
            else:
                max_best_peak = max(lambda x: x['peak_height'],
                                    filter(lambda y: y['peak_height'] < locus_parameters.offscale_threshold,
                                           best_annotation.annotated_peaks))
                max_curr_peak = max(lambda x: x['peak_height'],
                                    filter(lambda y: y['peak_height'] < locus_parameters.offscale_threshold,
                                           annotation.annotated_peaks))
                if max_curr_peak['peak_height'] > max_best_peak['peak_height']:
                    best_annotation = annotation
        return best_annotation

    def serialize(self):
        res = {
            'id': self.id,
            # 'sample_id': self.sample_id,
            'sample': self.sample.serialize(),
            'project_id': self.project_id,
            'moi': self.moi,
            'last_updated': self.last_updated,
        }
        return res

    def serialize_details(self):
        res = self.serialize()
        res.update({
            'locus_annotations': {x.locus_id: x.serialize() for x in self.locus_annotations}
        })
        return res


class SampleLocusAnnotation(TimeStamped, Flaggable, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sample_annotations_id = db.Column(db.Integer, db.ForeignKey("project_sample_annotations.id", ondelete="CASCADE"), index=True)
    locus_id = db.Column(db.Integer, db.ForeignKey('locus.id', ondelete="CASCADE"), index=True)
    locus = db.relationship('Locus', lazy='immediate')
    annotated_peaks = db.Column(MutableList.as_mutable(JSONEncodedData), default=[])
    reference_run_id = db.Column(db.Integer, db.ForeignKey('project_channel_annotations.id'), index=True)
    reference_run = db.relationship('ProjectChannelAnnotations', lazy='immediate')
    alleles = db.Column(MutableDict.as_mutable(JSONEncodedData))

    def serialize(self):
        res = {
            'id': self.id,
            'sample_annotations_id': self.sample_annotations_id,
            'locus_id': self.locus_id,
            'annotated_peaks': self.annotated_peaks,
            'reference_run_id': self.reference_run_id,
            'reference_channel_id': None,
            'alleles': self.alleles,
            'flags': self.flags,
            'comments': self.comments
        }

        if self.reference_run:
            res.update({
                'reference_channel_id': self.reference_run.channel_id,
            })

        return res


# Locus Set Association table
locus_set_association_table = db.Table('locus_set_association',
                                       db.Column('locus_id', db.Integer, db.ForeignKey('locus.id', ondelete="CASCADE")),
                                       db.Column('locus_set_id', db.Integer,
                                                 db.ForeignKey('locus_set.id', ondelete="CASCADE"))
                                       )


class LocusSet(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    loci = db.relationship('Locus', secondary=locus_set_association_table)
    label = db.Column(db.String(255), nullable=False)

    def __repr__(self):
        return "<LocusSet {}>".format(self.label)

    def serialize(self):
        res = {
            'id': self.id,
            'label': self.label,
            'loci': {locus.id: locus.serialize() for locus in self.loci}
        }
        return res


class Locus(Colored, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    label = db.Column(db.String(255), unique=True, nullable=False)
    max_base_length = db.Column(db.Integer, nullable=False)
    min_base_length = db.Column(db.Integer, nullable=False)
    nucleotide_repeat_length = db.Column(db.Integer, default=3, nullable=False)
    locus_metadata = db.Column(MutableDict.as_mutable(JSONEncodedData), default={}, nullable=False)

    def __repr__(self):
        return "<Locus {} {}>".format(self.label, self.color.capitalize())

    def serialize(self):
        res = {
            'id': self.id,
            'label': self.label,
            'max_base_length': self.max_base_length,
            'min_base_length': self.min_base_length,
            'nucleotide_repeat_length': self.nucleotide_repeat_length,
            'locus_matadata': self.locus_metadata,
            'color': self.color
        }
        return res


class Ladder(PeakScanner, Colored, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    label = db.Column(db.String(255), unique=True, nullable=False, index=True)
    base_sizes = db.Column(MutableList.as_mutable(JSONEncodedData), nullable=False)

    sq_limit = db.Column(db.Float, default=1, nullable=False)
    unusable_sq_limit = db.Column(db.Float, default=10, nullable=False)
    base_size_precision = db.Column(db.Integer, default=2, nullable=False)

    index_overlap = db.Column(db.Integer, default=15, nullable=False)
    min_time = db.Column(db.Integer, default=1200, nullable=False)
    max_peak_height = db.Column(db.Integer, default=12000, nullable=False)
    min_peak_height = db.Column(db.Integer, default=200, nullable=False)
    outlier_limit = db.Column(db.Integer, default=3, nullable=False)
    maximum_missing_peak_count = db.Column(db.Integer, default=5, nullable=False)
    allow_bleedthrough = db.Column(db.Boolean, default=True, nullable=False)
    remove_outliers = db.Column(db.Boolean, default=True, nullable=False)

    def __repr__(self):
        return "<Ladder {} {}>".format(self.label, self.color.capitalize())

    @property
    def filter_parameters(self):
        return {
            'index_overlap': self.index_overlap,
            'min_time': self.min_time,
            'max_peak_height': self.max_peak_height,
            'min_peak_height': self.min_peak_height,
            'outlier_limit': self.outlier_limit,
            'maximum_missing_peak_count': self.maximum_missing_peak_count,
            'allow_bleedthrough': self.allow_bleedthrough,
            'remove_outliers': self.remove_outliers,
        }

    def serialize(self):
        res = {
            'id': self.id,
            'label': self.label,
            'base_sizes': self.base_sizes,
            'sq_limit': self.sq_limit,
            'base_size_precision': self.base_size_precision,
            'color': self.color
        }
        res.update(self.filter_parameters)
        res.update(self.scanning_parameters)
        return res


class Plate(PlateExtractor, TimeStamped, Flaggable, db.Model):
    """
    Immutable data about plate sourced from zip of FSA Files
    """
    id = db.Column(db.Integer, primary_key=True)
    label = db.Column(db.String(255), unique=True, nullable=False, index=True)
    creator = db.Column(db.String(255), nullable=True)
    date_processed = db.Column(db.DateTime, default=datetime.utcnow)
    date_run = db.Column(db.Date, nullable=False)
    well_arrangement = db.Column(db.Integer, nullable=False, default=96)
    ce_machine = db.Column(db.String(255), default="Unknown")
    wells = db.relationship('Well', backref=db.backref('plate'), cascade='save-update, merge, delete, delete-orphan')
    plate_hash = db.Column(db.String(32), nullable=False, unique=True, index=True)

    def __repr__(self):
        return "<Plate {0}>".format(self.label)

    @validates('well_arrangement')
    def validate_well_arrangement(self, key, well_arrangement):
        assert well_arrangement in [96, 384]
        return well_arrangement

    @reconstructor
    def init_on_load(self):
        super(Plate, self).__init__(label=self.label, well_arrangement=self.well_arrangement, wells=self.wells,
                                    date_run=self.date_run, creator=self.creator, comments=self.comments,
                                    ce_machine=self.ce_machine, plate_hash=self.plate_hash)

    @classmethod
    def get_serialized_list(cls):
        plates = cls.query.values(cls.id, cls.label, cls.creator, cls.date_processed, cls.date_run,
                                  cls.well_arrangement, cls.ce_machine, cls.plate_hash, cls.last_updated)
        plates = [{'id': p[0],
                   'label': p[1],
                   'creator': p[2],
                   'date_processed': str(p[3]),
                   'date_run': str(p[4]),
                   'well_arrangement': str(p[5]),
                   'ce_machine': str(p[6]),
                   'plate_hash': str(p[7]),
                   'last_updated': str(p[8])} for p in plates]
        return plates

    @classmethod
    def from_zip(cls, zip_file, ladder_id, creator=None, comments=None, block_flush=False):
        extracted_plate = PlateExtractor.from_zip(zip_file, creator, comments)

        ladder = Ladder.query.get(ladder_id)
        extracted_plate = extracted_plate.calculate_base_sizes(ladder=ladder.base_sizes, color=ladder.color,
                                                               base_size_precision=ladder.base_size_precision,
                                                               sq_limit=ladder.sq_limit,
                                                               filter_parameters=ladder.filter_parameters,
                                                               scanning_parameters=ladder.scanning_parameters)

        p = cls(label=extracted_plate.label, comments=extracted_plate.comments, creator=extracted_plate.creator,
                date_run=extracted_plate.date_run, well_arrangement=extracted_plate.well_arrangement,
                ce_machine=extracted_plate.ce_machine, plate_hash=extracted_plate.plate_hash)

        db.session.add(p)
        db.session.flush()

        for well in extracted_plate.wells:
            w = Well(well_label=well.well_label, comments=well.comments, base_sizes=well.base_sizes,
                     ladder_peak_indices=well.ladder_peak_indices, sizing_quality=well.sizing_quality,
                     offscale_indices=well.offscale_indices, fsa_hash=well.fsa_hash)

            w.plate_id = p.id
            w.ladder_id = ladder.id
            db.session.add(w)
            db.session.flush()
            for channel in well.channels:
                c = Channel(wavelength=channel.wavelength, data=channel.data, color=channel.color)
                c.well_id = w.id
                db.session.add(c)
            db.session.flush()
        return p.id

    @classmethod
    def from_zips(cls, zip_files, ladder_id, creator=None, comments=None):
        plate_ids = []
        for z in zip_files:
            plate_ids.append(cls.from_zip(z, ladder_id, creator, comments))
        return plate_ids

    def load_plate_map(self, plate_map_file):
        r = csv.DictReader(plate_map_file)
        locus_labels = r.fieldnames
        print locus_labels
        locus_labels = [x for x in locus_labels if x.lower() not in ['', 'well']]
        print locus_labels
        for entry in r:
            print entry
            well_label = entry['Well']
            for locus_label in locus_labels:
                sample_barcode = entry[locus_label]
                sample = Sample.query.filter(Sample.barcode == sample_barcode).one()
                locus = Locus.query.filter(Locus.label == locus_label).one()
                well = self.wells_dict[well_label]
                channel = well.channels_dict[locus.color]
                if channel and locus and sample:
                    channel.add_locus(locus.id)
                    channel.add_sample(sample.id)
        return self

    def serialize(self):
        return {
            'id': self.id,
            'label': self.label,
            'creator': self.creator,
            'date_processed': str(self.date_processed),
            'date_run': str(self.date_run),
            'well_arrangement': self.well_arrangement,
            'ce_machine': self.ce_machine,
            'plate_hash': self.plate_hash,
            'last_updated': str(self.last_updated),
            'wells': {w.well_label: w.serialize() for w in self.wells}
        }


class Well(WellExtractor, TimeStamped, Flaggable, db.Model):
    """
    Immutable data about well sourced from FSA Files, apart from ladder used.
    """
    id = db.Column(db.Integer, primary_key=True)
    plate_id = db.Column(db.Integer, db.ForeignKey("plate.id", ondelete="CASCADE"), nullable=False)
    well_label = db.Column(db.String(3), nullable=False)
    base_sizes = deferred(db.Column(MutableList.as_mutable(JSONEncodedData)))
    ladder_peak_indices = db.Column(MutableList.as_mutable(JSONEncodedData))
    sizing_quality = db.Column(db.Float, default=1000)
    channels = db.relationship('Channel', backref=db.backref('well'),
                               cascade='save-update, merge, delete, delete-orphan')
    offscale_indices = db.Column(MutableList.as_mutable(JSONEncodedData))
    ladder_id = db.Column(db.Integer, db.ForeignKey('ladder.id'), nullable=False)
    ladder = db.relationship('Ladder')
    fsa_hash = db.Column(db.String(32), nullable=False, unique=True, index=True)

    def __repr__(self):
        if self.sizing_quality:
            return "<Well {0} {1}>".format(self.well_label, round(self.sizing_quality, 2))
        else:
            return "<Well {0}>".format(self.well_label)

    @reconstructor
    def init_on_load(self):
        super(Well, self).__init__(well_label=self.well_label, comments=self.comments, base_sizes=self.base_sizes,
                                   sizing_quality=self.sizing_quality, offscale_indices=self.offscale_indices,
                                   ladder_peak_indices=self.ladder_peak_indices, channels=self.channels,
                                   fsa_hash=self.fsa_hash)

    def calculate_base_sizes(self, peak_indices=None):
        ladder = self.ladder.base_sizes
        color = self.ladder.color
        base_size_precision = self.ladder.base_size_precision
        sq_limit = self.ladder.sq_limit
        filter_parameters = self.ladder.filter_parameters
        scanning_parameters = self.ladder.scanning_parameters
        super(Well, self).calculate_base_sizes(ladder=ladder, color=color, peak_indices=peak_indices,
                                               base_size_precision=base_size_precision,
                                               sq_limit=sq_limit, filter_parameters=filter_parameters,
                                               scanning_parameters=scanning_parameters)
        for channel in self.channels:
            channel.annotate_base_sizes()
        return self

    def serialize(self):
        return {
            'id': self.id,
            'plate_id': self.plate_id,
            'well_label': self.well_label,
            'sizing_quality': self.sizing_quality,
            'last_updated': str(self.last_updated),
            'offscale_indices': self.offscale_indices,
            'ladder_id': self.ladder_id,
            'fsa_hash': self.fsa_hash,
            'channels': {channel.color: channel.serialize() for channel in self.channels},
            'ladder_peak_indices': None,
            'base_sizes': None
        }

    def serialize_details(self):
        res = self.serialize()
        res.update({
            'ladder_peak_indices': self.ladder_peak_indices,
            'base_sizes': self.base_sizes
        })
        return res


class Channel(ChannelExtractor, TimeStamped, Colored, Flaggable, db.Model):
    """
    Immutable data about channel within an FSA File
    """
    id = db.Column(db.Integer, primary_key=True)
    well_id = db.Column(db.Integer, db.ForeignKey("well.id", ondelete="CASCADE"))
    wavelength = db.Column(db.Integer, nullable=False)
    data = deferred(db.Column(MutableList.as_mutable(JSONEncodedData)))
    max_data_point = db.Column(db.Integer, default=0)

    sample_id = db.Column(db.Integer, db.ForeignKey('sample.id'))
    locus_id = db.Column(db.Integer, db.ForeignKey('locus.id'))
    locus = db.relationship('Locus')

    def __repr__(self):
        if self.locus:
            return "<Channel {} {}>".format(self.locus.label, self.color.capitalize())
        else:
            return "<Channel {}>".format(self.color)

    @reconstructor
    def init_on_load(self):
        super(Channel, self).__init__(color=self.color, wavelength=self.wavelength)

    def annotate_base_sizes(self):
        base_size_annotator = self.well.base_size_annotator()
        self.pre_annotate_peak_indices(base_size_annotator)
        return self

    def annotate_bleedthrough(self, idx_dist=1):
        bleedthrough_annotator = self.well.bleedthrough_annotator(color=self.color, idx_dist=idx_dist)
        self.pre_annotate_peak_indices(bleedthrough_annotator)
        return self

    def annotate_crosstalk(self, max_capillary_distance=2, idx_dist=1):
        crosstalk_annotator = self.well.plate.crosstalk_annotator(well_label=self.well.well_label, color=self.color,
                                                                  max_capillary_distance=max_capillary_distance,
                                                                  idx_dist=idx_dist)
        self.pre_annotate_peak_indices(crosstalk_annotator)
        return self

    def filter_to_locus_range(self):
        self.filter_annotated_peaks(
            base_size_filter(min_size=self.locus.min_base_length, max_size=self.locus.max_base_length))

    def pre_annotate_and_filter(self, filter_params):
        self.annotate_base_sizes()
        self.filter_to_locus_range()
        self.annotate_peak_heights()
        self.filter_annotated_peaks(peak_height_filter(min_height=filter_params['min_peak_height'],
                                                       max_height=filter_params['max_peak_height']))
        self.annotate_bleedthrough()
        self.filter_annotated_peaks(bleedthrough_filter(max_bleedthrough_ratio=filter_params['max_bleedthrough']))
        self.annotate_crosstalk()
        self.filter_annotated_peaks(crosstalk_filter(max_crosstalk_ratio=filter_params['max_crosstalk']))
        self.filter_annotated_peaks(peak_proximity_filter(min_peak_distance=filter_params['min_peak_distance']))
        self.annotate_peak_area()

    def post_annotate_peaks(self):
        self.annotate_relative_peak_heights()
        self.annotate_relative_peak_area()

    def post_filter_peaks(self, filter_params):
        self.filter_annotated_peaks(
            relative_peak_height_filter(min_relative_peak_height=filter_params['min_peak_height_ratio']))

    def add_sample(self, sample_id, block_commit=False):
        self.sample_id = sample_id
        # if self.locus_id:
        #     projects = GenotypingProject.query.join(ProjectSampleAnnotations).filter(
        #         ProjectSampleAnnotations.sample_id == sample_id).all()
        #     for project in projects:
        #         if self.locus in project.locus_set.loci:
        #             project.add_channel(self.id, block_commit=block_commit)
        return self

    def add_locus(self, locus_id, block_commit=False):
        locus = Locus.query.get(locus_id)
        self.locus = locus
        self.locus_id = locus_id
        self.find_max_data_point()
        # if self.sample_id:
        #     projects = GenotypingProject.query.join(ProjectSampleAnnotations).filter(
        #         ProjectSampleAnnotations.sample_id == self.sample_id).all()
        #     for project in projects:
        #         if self.locus in project.locus_set.loci:
        #             project.add_channel(self.id, block_commit=block_commit)
        return self

    def find_max_data_point(self):
        if self.locus and self.well.ladder_peak_indices:
            self.well.ladder_peak_indices.sort()
            j = 0
            while self.well.base_sizes[self.well.ladder_peak_indices[j]] < self.locus.min_base_length:
                j += 1
            i = self.well.ladder_peak_indices[j - 1]
            while self.well.base_sizes[i] < self.locus.max_base_length:
                i += 1
                if self.well.base_sizes[i] > self.locus.min_base_length:
                    if self.data[i] > self.max_data_point:
                        self.max_data_point = self.data[i]

    def serialize(self):
        res = {
            'id': self.id,
            'well_id': self.well_id,
            'wavelength': self.wavelength,
            'sample_id': self.sample_id,
            'locus_id': self.locus_id,
            'max_data_point': self.max_data_point,
            'data': None
        }
        return res

    def serialize_details(self):
        res = self.serialize()
        res.update({
            'data': self.data
        })
        return res