"""Searching a folder and everything below it: the `restic find` pattern, and which matches count."""

from jungbackup.backup.search import Query


def test_pattern_is_anchored_at_the_folder():
    assert Query(" resume ", "/home/me").pattern == "/home/me/**/*resume*"
    assert Query("resume", "/").pattern == "/**/*resume*"
    assert Query("*.pdf", "/home/me").pattern == "/home/me/**/*.pdf"
    assert Query("jobs/resume", "/home/me").pattern == "/home/me/**/*jobs/resume*"
    assert Query("x", "/d [1]*").pattern == "/d \\[1]\\*/**/*x*"     # the folder is literal


def test_short_text_is_not_searched():
    assert not Query("r", "/").valid and not Query("  ", "/").valid
    assert Query("re", "/").valid and Query("*", "/").valid


def test_only_matching_names_count_not_everything_in_a_matching_folder():
    q = Query("resume", "/home/me")
    assert q.matches("/home/me/Documents/Resume-2024.pdf")
    assert q.matches("/home/me/Resumes")
    assert not q.matches("/home/me/Resumes/cover.odt")
    assert not q.matches("/home/me")                         # the folder itself
    assert not q.matches("/home/resume.txt")                 # above it


def test_globs_match_whole_names_and_slashes_the_path_end():
    pdf = Query("*.PDF", "/h")
    assert pdf.matches("/h/a/Resume.pdf") and not pdf.matches("/h/a.pdf.txt")
    jobs = Query("jobs/res", "/h")
    assert jobs.matches("/h/jobs/Resume.pdf") and jobs.matches("/h/old/jobs/resume")
    assert not jobs.matches("/h/jobs/old/cover.odt")


def test_results_are_named_like_ls_nodes():
    data = [{"snapshot": "1", "matches": [{"path": "/h/Resumes", "type": "dir"},
                                         {"path": "/h/Resumes/cover.odt", "type": "file"},
                                         {"path": "/h/Resumes/resume.pdf", "type": "file", "size": 3}]}]
    assert Query("resume", "/h").results(data) == [
        {"path": "/h/Resumes", "type": "dir", "name": "Resumes"},
        {"path": "/h/Resumes/resume.pdf", "type": "file", "size": 3, "name": "resume.pdf"},
    ]
    assert Query("resume", "/h").results(None) == []
