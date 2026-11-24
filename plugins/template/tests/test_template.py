from throughline_template import Template


def test_plg_2_shared_generator():
    assert callable(Template.records)
