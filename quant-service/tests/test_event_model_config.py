import pytest
from app.event_research.model_config import settings

def clear(monkeypatch):
    for key in ('API_BASE','MODEL','API_KEY','API_KEY_FILE','API_KEY_REF','THINKING'):
        monkeypatch.delenv('EVENT_RESEARCH_'+key,raising=False)

def test_external_reference_and_thinking(monkeypatch,tmp_path):
    clear(monkeypatch)
    file=tmp_path/'credentials.yaml';file.write_text('refs:\n  DEEPSEEK_API_KEY: test-only\n')
    monkeypatch.setenv('EVENT_RESEARCH_API_BASE','https://api.deepseek.com/')
    monkeypatch.setenv('EVENT_RESEARCH_MODEL','deepseek-flash')
    monkeypatch.setenv('EVENT_RESEARCH_API_KEY_FILE',str(file))
    monkeypatch.setenv('EVENT_RESEARCH_THINKING','disabled')
    assert settings()==('https://api.deepseek.com','test-only','deepseek-flash',{'thinking':{'type':'disabled'}})

def test_explicit_env_precedes_file(monkeypatch):
    clear(monkeypatch)
    monkeypatch.setenv('EVENT_RESEARCH_API_KEY','test-only')
    monkeypatch.setenv('EVENT_RESEARCH_API_KEY_FILE','does-not-exist')
    assert settings()[1]=='test-only'

def test_missing_reference_and_bad_option(monkeypatch,tmp_path):
    clear(monkeypatch)
    file=tmp_path/'credentials.yaml';file.write_text('refs: {}')
    monkeypatch.setenv('EVENT_RESEARCH_API_KEY_FILE',str(file))
    assert settings()[1]==''
    monkeypatch.setenv('EVENT_RESEARCH_THINKING','bad')
    with pytest.raises(ValueError):settings()

def test_missing_expectation_only_removes_certainty():
    from app.event_research.analysis import normalize_model_uncertainty
    result={'events':[{'expectation':'unknown','surprise':'positive'},
        {'expectation':'Document says consensus growth 40%','surprise':'negative'}]}
    changes=normalize_model_uncertainty(result)
    assert len(changes)==1
    assert result['events'][0]=={'expectation':'unknown','surprise':'unknown'}
    assert result['events'][1]['surprise']=='negative'
