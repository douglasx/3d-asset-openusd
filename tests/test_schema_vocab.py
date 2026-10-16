from pxr import Sdf, Usd
from rdflib import OWL, RDF, Graph, Namespace

from marinekit import vocab
from marinekit.paths import ONTOLOGY, SCHEMA_PLUGIN

MO = Namespace("https://example.org/marine/ontology#")


def test_codeless_schema_registers_every_attribute():
    d = Usd.SchemaRegistry().FindAppliedAPIPrimDefinition("MarineAssetAPI")
    assert d is not None, "PXR_PLUGINPATH_NAME must point at schema/marine"
    layer = Sdf.Layer.FindOrOpen(str(SCHEMA_PLUGIN / "schema.usda"))
    declared = set(layer.GetPrimAtPath("/MarineAssetAPI").properties.keys())
    assert declared == set(d.GetPropertyNames())


def test_every_vocab_class_is_in_the_ontology():
    g = Graph().parse(ONTOLOGY / "marine.ttl")
    owl_classes = {str(s).rsplit("#", 1)[-1] for s in g.subjects(RDF.type, OWL.Class)}
    missing = set(vocab.classes()) - owl_classes
    assert not missing, f"add to marine.ttl: {missing}"


def test_required_attrs_inherit_through_class_parents():
    assert {"diameter", "length"} <= set(vocab.required_attrs("Shaft"))
    assert "code" in vocab.required_attrs("Propeller")


def test_materials_have_density_and_colour():
    for k, m in vocab.materials().items():
        assert m["density"] >= 0 and m["color"].startswith("#"), k
