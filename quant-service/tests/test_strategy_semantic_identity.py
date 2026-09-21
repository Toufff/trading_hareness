from app.strategy_governance.semantic_identity import semantic_source, lane_profile


def test_unrelated_lane_change_does_not_split_a_cohort():
    before = "matches = {'trend': (x > 5, x), 'pullback': (x > 3, x)}"
    after = before.replace('x > 3', 'x > 4')
    assert semantic_source(before, 'trend') == semantic_source(after, 'trend')
    assert semantic_source(before, 'pullback') != semantic_source(after, 'pullback')


def test_comments_and_documentation_do_not_change_decisions():
    assert semantic_source('"doc"\nx=1 # detail', 'trend') == semantic_source('"new doc"\nx=1', 'trend')


def test_build_revision_is_not_economic_identity():
    one = {'version':'report-v1','strategy_code_hash':'build-a', 'settings':{},'decision_versions':{'trend':'decision-a'}}
    two = {**one,'version':'report-v2','strategy_code_hash':'build-b'}
    assert lane_profile(one, 'trend') == lane_profile(two, 'trend')
    assert lane_profile({**two,'decision_versions':{'trend':'decision-b'}}, 'trend') != lane_profile(one, 'trend')


def test_legacy_cannot_be_silently_merged():
    base={'version':'v1','strategy_code_hash':'legacy','settings':{}}
    assert lane_profile(base,'trend') != lane_profile({**base,'decision_versions':{'trend':'legacy'}},'trend')
