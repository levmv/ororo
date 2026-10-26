import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ororo as od


class InputFileTests(unittest.TestCase):
    def test_file_and_url_arguments_preserve_order_and_ignore_duplicates(self):
        breaking_bad = "https://ororo.tv/en/shows/breaking-bad"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "list.txt"
            path.write_bytes((
                "\ufeff# Shows\r\n\r\n"
                "  https://ororo.tv/en/shows/heroes  \r\n"
                f"{breaking_bad}\r\n"
                "  # Movies\r\n"
                "https://ororo.tv/en/movies/the-martian\r\n"
                "https://www.ororo.tv/ru/shows/heroes\r\n"
            ).encode("utf-8"))
            pages = od.pages_from_inputs([breaking_bad], path)
        self.assertEqual(
            [(page.kind, page.slug) for page in pages],
            [("shows", "breaking-bad"), ("shows", "heroes"), ("movies", "the-martian")],
        )

    def test_invalid_line_reports_location_before_authentication(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "list.txt"
            for invalid in (
                "https://example.org/shows/heroes",
                "https://ororo.tv/en/shows/heroes https://ororo.tv/en/movies/the-martian",
            ):
                with self.subTest(invalid=invalid):
                    path.write_text(
                        "https://ororo.tv/en/shows/heroes\n# Another entry\n" + invalid + "\n",
                        encoding="utf-8",
                    )
                    errors = io.StringIO()
                    with mock.patch.object(od, "load_credentials") as credentials:
                        with contextlib.redirect_stderr(errors):
                            result = od.main(["-i", str(path)])
                    self.assertEqual(result, 2)
                    self.assertIn(f"{path}:3:", errors.getvalue())
                    credentials.assert_not_called()

    def test_empty_input_fails_before_authentication(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "empty.txt"
            for contents, args in (
                ("", []),
                ("", ["-i", str(path)]),
                ("\n # Nothing selected\n", ["-i", str(path)]),
            ):
                with self.subTest(contents=contents, args=args):
                    path.write_text(contents, encoding="utf-8")
                    errors = io.StringIO()
                    with mock.patch.object(od, "load_credentials") as credentials:
                        with contextlib.redirect_stderr(errors):
                            result = od.main(args)
                    self.assertEqual(result, 2)
                    self.assertIn("no URLs supplied", errors.getvalue())
                    credentials.assert_not_called()

    def test_unreadable_input_file_has_a_clear_error(self):
        with tempfile.TemporaryDirectory() as directory:
            invalid = Path(directory) / "invalid-utf8.txt"
            invalid.write_bytes(b"\xff")
            for path in (Path(directory) / "missing.txt", invalid):
                with self.subTest(path=path):
                    with self.assertRaises(od.DownloadError) as error:
                        od.pages_from_inputs([], path)
                    self.assertIn(f"could not read input file {path}", str(error.exception))

    def test_cli_accepts_file_or_url_and_preserves_dry_run(self):
        url = "https://ororo.tv/en/movies/the-martian"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "list.txt"
            path.write_text(url + "\n", encoding="utf-8")
            output = Path(directory) / "archive"
            for inputs in (["-i", str(path)], ["--input-file", str(path)], [url]):
                with self.subTest(inputs=inputs), contextlib.ExitStack() as stack:
                    stack.enter_context(mock.patch.dict(os.environ, {}, clear=True))
                    stack.enter_context(mock.patch.object(od, "load_config", return_value={}))
                    stack.enter_context(mock.patch.object(
                        od, "load_credentials", return_value=("user@example.com", "secret")
                    ))
                    api_class = stack.enter_context(mock.patch.object(od, "OroroAPI"))
                    api = api_class.return_value
                    api.find.return_value = {"id": 1, "name": "The Martian", "year": "2015"}
                    preview = stack.enter_context(mock.patch.object(od, "dry_run"))
                    preview.return_value = {"items": [], "errors": []}
                    download = stack.enter_context(mock.patch.object(od, "download_all"))
                    result = od.main(inputs + ["-o", str(output), "--dry-run"])
                    self.assertEqual(result, 0)
                    api_class.assert_called_once_with("user@example.com", "secret")
                    api.find.assert_called_once_with(od.parse_page_url(url))
                    preview.assert_called_once()
                    preview_api, targets, subs, quality = preview.call_args.args
                    self.assertIs(preview_api, api)
                    self.assertEqual(len(targets), 1)
                    self.assertEqual(
                        targets[0].video_path,
                        output / "The Martian (2015)" / "The Martian (2015).mp4",
                    )
                    self.assertEqual(subs, {"en", "ru"})
                    self.assertIsNone(quality)
                    download.assert_not_called()
                    self.assertFalse(output.exists())


class NamingTests(unittest.TestCase):
    def test_page_urls(self):
        self.assertEqual(od.parse_page_url("https://ororo.tv/en/shows/heroes").kind, "shows")
        self.assertEqual(od.parse_page_url("https://ororo.tv/en/movies/the-martian").slug, "the-martian")

    def test_slug_and_safe_name(self):
        self.assertEqual(od.slugify("Marvel's Agents of S.H.I.E.L.D."), "marvel-s-agents-of-s-h-i-e-l-d")
        self.assertEqual(od.safe_name('A/B: C?'), "AB C")

    def test_number_ranges(self):
        self.assertEqual(od.parse_number_set(["1,3-5", "9"]), {1, 3, 4, 5, 9})
        self.assertIsNone(od.parse_number_set(None))
        for invalid in ("abc", "-3", "1-", "5-2", "1-2-3"):
            with self.subTest(invalid=invalid), self.assertRaises(od.DownloadError):
                od.parse_number_set([invalid])

    def test_chosen_quality_matches_download_path(self):
        detail = {
            "resolution": "HD",
            "download_url": "https://static.example/movie_2160p.mp4?signature=secret",
            "versions": {
                "1080p": "/files/movie_1080p.mp4",
                "2160p": "/movie_2160p.mp4",
            },
        }
        self.assertEqual(od.chosen_quality(detail, "unknown"), "2160p")

    def test_hls_variant_parsing(self):
        master = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=1000000,CODECS="avc1,mp4a",RESOLUTION=1280x720
video__720p.mp4_video.m3u8?token=x
#EXT-X-STREAM-INF:BANDWIDTH=3000000,RESOLUTION=1920x1080
video__1080p.mp4_video.m3u8?token=x
"""
        variants = od.parse_hls_variants("https://media.example/master.m3u8", master)
        self.assertEqual([(item.program_id, item.quality) for item in variants], [(0, "720p"), (1, "1080p")])
        self.assertEqual(variants[1].bandwidth, 3_000_000)
        self.assertEqual(variants[0].url, "https://media.example/video__720p.mp4_video.m3u8?token=x")

    def test_hls_duration_and_quality_ceiling(self):
        self.assertAlmostEqual(od.hls_duration("#EXTINF:4.004,\npart\n#EXTINF:1.5,\npart\n"), 5.504)
        self.assertEqual(od.parse_max_quality("1080p"), 1080)
        self.assertIsNone(od.parse_max_quality("best"))
        self.assertEqual(od.quality_from_dimensions(1920), "1080p")

    def test_show_layout_and_filters(self):
        class FakeAPI:
            def find(self, page):
                return {"id": 35}

            def get_json(self, path):
                return {
                    "name": "Heroes",
                    "year": "2006",
                    "episodes": [
                        {"id": 1, "season": 1, "number": "1", "name": "Genesis", "resolution": "SD"},
                        {"id": 2, "season": 2, "number": "3", "name": "Kindred", "resolution": "SD"},
                    ],
                }

        with tempfile.TemporaryDirectory() as directory:
            targets = od.targets_for_page(
                FakeAPI(),
                od.parse_page_url("https://ororo.tv/en/shows/heroes"),
                Path(directory),
                {2},
                {3},
            )
            self.assertEqual(len(targets), 1)
            self.assertEqual(
                targets[0].video_path.relative_to(directory),
                Path("Heroes (2006)/Season 02/Heroes - S02E03 - Kindred.mp4"),
            )

    def test_video_resumes_partial_file(self):
        class Response(io.BytesIO):
            status = 206
            headers = {"Content-Range": "bytes 3-5/6"}

            def getcode(self):
                return self.status

        original_request = od.request_url
        calls = []

        def fake_request(url, *, method="GET", start=None, end=None):
            calls.append((method, start, end))
            return Response(b"def")

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "episode.mp4"
            destination.with_suffix(".mp4.part").write_bytes(b"abc")
            source = od.MediaSource("direct", "https://example.test/video", "unknown", 6, True)
            od.save_resume_metadata(destination.with_suffix(".mp4.part.json"), source)
            try:
                od.request_url = fake_request
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    od.download_video("https://example.test/video", destination, 6, "episode")
            finally:
                od.request_url = original_request

            self.assertEqual(calls, [("GET", 3, None)])
            self.assertEqual(destination.read_bytes(), b"abcdef")
            self.assertFalse(destination.with_suffix(".mp4.part").exists())

    def test_existing_lower_quality_is_upgraded(self):
        source = od.MediaSource("hls", "https://example.test/master", "1080p", 100, False, 4)
        original_probe = od.local_quality
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "movie.mp4"
            path.write_bytes(b"existing-low-quality-file")
            try:
                od.local_quality = lambda unused: "720p"
                self.assertEqual(od.existing_action(path, source), ("upgrade", "720p"))
                od.local_quality = lambda unused: "2160p"
                self.assertEqual(od.existing_action(path, source), ("skip", "2160p"))
            finally:
                od.local_quality = original_probe

    def test_exact_existing_download_skips_without_probe(self):
        source = od.MediaSource("direct", "https://example.test/video", "480p", 3, True)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "episode.mp4"
            path.write_bytes(b"abc")
            self.assertEqual(od.existing_action(path, source), ("skip", "480p"))

    def test_credentials_round_trip_with_private_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.json"
            od.save_credentials(path, "user@example.com", "secret")
            self.assertEqual(od.load_credentials(path), ("user@example.com", "secret"))
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)


class Response(io.BytesIO):
    def __init__(self, data=b"", status=200, headers=None):
        super().__init__(data)
        self.status = status
        self.headers = headers or {}

    def getcode(self):
        return self.status


class IsolatedTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = self.root / "config.json"
        self.config.write_text("{}", encoding="utf-8")
        self.credentials = self.root / "credentials.json"
        self.env = mock.patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        network = mock.patch.object(od, "urlopen", side_effect=AssertionError("unexpected network access"))
        network.start()
        self.addCleanup(network.stop)

    def cli(self, args, api=None, env=None):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stdout(output))
            stack.enter_context(contextlib.redirect_stderr(errors))
            stack.enter_context(mock.patch.dict(os.environ, env or {}, clear=True))
            if api is not None:
                stack.enter_context(mock.patch.object(od, "OroroAPI", return_value=api))
            result = od.main([*args, "--config", str(self.config), "--credentials-file", str(self.credentials)])
        return result, output.getvalue(), errors.getvalue()

    def target(self, index=1):
        return od.Target("movie", index, self.root / str(index), "movie", f"movie {index}", "1080p")

    def partial(self, destination, source, data=b"abc"):
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = od.partial_path(destination)
        partial.write_bytes(data)
        od.save_resume_metadata(partial.with_suffix(partial.suffix + ".json"), source)
        return partial


class CommandTests(IsolatedTests):
    url = "https://ororo.tv/en/movies/the-martian"
    env_credentials = {"ORORO_USER": "demo@example.com", "ORORO_PASSWORD": "demo-password"}

    def test_missing_credentials_never_prompt_in_json_mode(self):
        with mock.patch("builtins.input") as prompt, mock.patch.object(od.getpass, "getpass") as password:
            code, output, errors = self.cli([self.url, "--json"])
        self.assertEqual(code, 2)
        self.assertIn("ororo login", json_load(output)["error"])
        prompt.assert_not_called()
        password.assert_not_called()
        self.assertNotIn("Traceback", errors)

    def test_non_interactive_mode_does_not_prompt_even_with_a_terminal(self):
        with mock.patch.object(sys.stdin, "isatty", return_value=True), mock.patch("builtins.input") as prompt:
            code, _, errors = self.cli([self.url, "--non-interactive"])
        self.assertEqual(code, 2)
        self.assertIn("credentials are missing", errors)
        prompt.assert_not_called()

    def test_environment_credentials_bypass_invalid_saved_file(self):
        self.credentials.write_text("[]", encoding="utf-8")
        api = mock.Mock()
        api.catalog.return_value = []
        code, output, _ = self.cli(["search", "anything", "--json"], api, self.env_credentials)
        self.assertEqual(code, 0)
        self.assertTrue(json_load(output)["ok"])
        self.assertEqual(self.credentials.read_text(), "[]")

    def test_wrong_credential_json_type_is_a_controlled_error(self):
        for content in ("[]", "null", '"text"', "12"):
            with self.subTest(content=content):
                self.credentials.write_text(content, encoding="utf-8")
                self.credentials.chmod(0o600)
                code, output, errors = self.cli([self.url, "--json"])
                self.assertEqual(code, 2)
                self.assertIn("JSON object", json_load(output)["error"])
                self.assertNotIn("Traceback", errors)

    def test_login_verifies_then_saves_private_credentials(self):
        api = mock.Mock()
        api.catalog.return_value = []
        code, output, _ = self.cli(["login", "--json"], api, self.env_credentials)
        self.assertEqual(code, 0)
        api.catalog.assert_called_once_with("movies")
        self.assertEqual(od.load_credentials(self.credentials), ("demo@example.com", "demo-password"))
        self.assertEqual(self.credentials.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("demo-password", output)

    def test_rejected_login_does_not_replace_credentials(self):
        od.save_credentials(self.credentials, "old@example.com", "old")
        api = mock.Mock()
        api.catalog.side_effect = od.AuthenticationError("rejected")
        code, output, _ = self.cli(["login", "--json"], api, self.env_credentials)
        self.assertEqual(code, 2)
        self.assertIn("ororo login", json_load(output)["error"])
        self.assertEqual(od.load_credentials(self.credentials), ("old@example.com", "old"))

    def test_login_network_failure_is_reported_without_replacing_credentials(self):
        od.save_credentials(self.credentials, "old@example.com", "old")

        def timeout(*args, **kwargs):
            self.assertIn("Verifying ORORO credentials", sys.stderr.getvalue())
            raise TimeoutError("The read operation timed out")

        with mock.patch.object(od, "urlopen", side_effect=timeout) as opener:
            code, output, errors = self.cli(["login", "--json"], env=self.env_credentials)
        self.assertEqual(code, 2)
        self.assertEqual(opener.call_count, len(od.API_DOMAINS))
        self.assertEqual(len(output.splitlines()), 1)
        self.assertFalse(json_load(output)["ok"])
        self.assertIn("HTTPS_PROXY", json_load(output)["error"])
        for domain in od.API_DOMAINS:
            self.assertIn(domain, json_load(output)["error"])
        self.assertNotIn("demo-password", output + errors)
        self.assertNotIn("saved credentials", errors)
        self.assertEqual(od.load_credentials(self.credentials), ("old@example.com", "old"))

    def test_login_can_repair_a_malformed_file(self):
        self.credentials.write_text("[]", encoding="utf-8")
        api = mock.Mock()
        api.catalog.return_value = []
        with mock.patch.object(sys.stdin, "isatty", return_value=True), mock.patch.object(od.getpass, "getpass", return_value="new-password"):
            code, _, _ = self.cli(["login", "--user", "demo@example.com"], api)
        self.assertEqual(code, 0)
        self.assertEqual(od.load_credentials(self.credentials), ("demo@example.com", "new-password"))

    def test_rejected_saved_password_does_not_prompt_unattended(self):
        od.save_credentials(self.credentials, "demo@example.com", "old")
        api = mock.Mock()
        api.find.side_effect = od.AuthenticationError("rejected")
        with mock.patch.object(od.getpass, "getpass") as prompt:
            code, output, _ = self.cli([self.url, "--json"], api)
        self.assertEqual(code, 2)
        self.assertIn("ororo login", json_load(output)["error"])
        prompt.assert_not_called()

    def test_search_is_case_insensitive_and_filters_catalog(self):
        api = mock.Mock()
        api.catalog.return_value = [
            {"id": 1, "name": "The Martian", "year": "2015", "slug": "the-martian"},
            {"id": 2, "name": "Other", "year": "2015"},
        ]
        code, output, _ = self.cli(["--json", "search", "MARTIAN", "2015", "--type", "movies"], api, self.env_credentials)
        self.assertEqual(code, 0)
        result = json_load(output)
        self.assertEqual([item["name"] for item in result["items"]], ["The Martian"])
        self.assertEqual(result["items"][0]["url"], self.url)
        api.catalog.assert_called_once_with("movies")

    def test_info_lists_filtered_episodes_without_signed_media_urls(self):
        api = mock.Mock()
        api.find.return_value = {"id": 35, "name": "Heroes"}
        api.get_json.return_value = {"name": "Heroes", "description": "A show", "episodes": [
            {"id": 2, "season": 2, "number": 1, "name": "Later"},
            {"id": 1, "season": 1, "number": 2, "name": "Selected"},
        ], "download_url": "https://media.example/video?token=private"}
        code, output, _ = self.cli(["info", "https://ororo.tv/en/shows/heroes", "--season", "1", "--json"], api, self.env_credentials)
        self.assertEqual(code, 0)
        item = json_load(output)["items"][0]
        self.assertEqual(item["seasons"], [1])
        self.assertEqual(item["episodes"][0]["name"], "Selected")
        self.assertNotIn("private", output)

    def test_settings_supply_defaults_and_cli_overrides_them(self):
        self.config.write_text(od.json.dumps({"output": str(self.root / "default"), "max_quality": "720p", "subs": "en", "delay": 0}))
        api = mock.Mock()
        api.find.return_value = {"id": 1, "name": "The Martian", "year": "2015"}
        with mock.patch.object(od, "dry_run", return_value={"errors": []}) as preview:
            code, _, _ = self.cli([self.url, "--dry-run", "--max-quality", "1080p", "-o", str(self.root / "override")], api, self.env_credentials)
        self.assertEqual(code, 0)
        _, targets, subs, quality = preview.call_args.args
        self.assertEqual(targets[0].directory.parent, self.root / "override")
        self.assertEqual(subs, {"en"})
        self.assertEqual(quality, 1080)

    def test_bad_options_fail_before_authentication(self):
        for options in (["--delay", "-1"], ["--delay", "nan"], ["--delay", "inf"], ["--max-quality", "900p"], ["--unknown"]):
            with self.subTest(options=options), mock.patch.object(od, "resolve_credentials") as credentials:
                code, output, errors = self.cli([self.url, "--json", *options])
                self.assertEqual(code, 2)
                self.assertFalse(json_load(output)["ok"])
                self.assertNotIn("Traceback", errors)
                credentials.assert_not_called()

    def test_login_rejects_dry_run_instead_of_saving_anything(self):
        code, output, _ = self.cli(["login", "--dry-run", "--json"], env=self.env_credentials)
        self.assertEqual(code, 2)
        self.assertIn("only available for downloads", json_load(output)["error"])
        self.assertFalse(self.credentials.exists())

    def test_json_download_has_one_result_and_no_progress_noise(self):
        api = mock.Mock()
        api.find.return_value = {"id": 1, "name": "The Martian", "year": "2015"}
        api.detail.return_value = {"download_url": "https://media.example/movie.mp4?token=private", "resolution": "1080p"}
        response = Response(b"abcdef", headers={"Content-Length": "6", "ETag": '"v1"'})
        with mock.patch.object(od, "probe_source", return_value=(6, '"v1"', None)), mock.patch.object(od, "request_url", return_value=response):
            code, output, errors = self.cli([self.url, "--json", "--subs", "none", "-o", str(self.root / "archive")], api, self.env_credentials)
        self.assertEqual(code, 0)
        result = json_load(output)
        self.assertTrue(result["ok"])
        self.assertEqual(Path(result["items"][0]["path"]).read_bytes(), b"abcdef")
        self.assertNotIn("private", output)
        self.assertNotIn("\r", errors)
        self.assertEqual(len(output.splitlines()), 1)

    def test_keep_going_reports_a_failed_page_and_plans_the_next(self):
        api = mock.Mock()
        api.find.side_effect = [od.DownloadError("unavailable"), {"id": 2, "name": "Other"}]
        api.detail.return_value = {"subtitles": []}
        source = od.MediaSource("direct", "https://media.example/video", "1080p", 6, True)
        with mock.patch.object(od, "select_source", return_value=source):
            code, output, _ = self.cli([self.url, "https://ororo.tv/en/movies/other", "--dry-run", "--keep-going", "--json", "-o", str(self.root / "archive")], api, self.env_credentials)
        self.assertEqual(code, 1)
        result = json_load(output)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(len(result["items"]), 1)
        self.assertFalse((self.root / "archive").exists())

    def test_new_credential_directory_is_private(self):
        path = self.root / "new-config" / "ororo" / "credentials.json"
        od.save_credentials(path, "demo", "demo")
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def test_bad_config_fails_before_authentication(self):
        for config in ([], {"outpt": "typo"}, {"output": 42}, {"delay": True}):
            with self.subTest(config=config), mock.patch.object(od, "resolve_credentials") as credentials:
                self.config.write_text(od.json.dumps(config))
                code, output, _ = self.cli([self.url, "--json"])
                self.assertEqual(code, 2)
                self.assertFalse(json_load(output)["ok"])
                credentials.assert_not_called()

    def test_dry_run_network_failure_is_a_json_error(self):
        api = mock.Mock()
        api.find.return_value = {"id": 1, "name": "The Martian", "year": "2015"}
        api.detail.return_value = {"download_url": "https://media.example/movie.mp4", "resolution": "1080p"}
        with mock.patch.object(od, "request_url", side_effect=od.URLError("offline")), mock.patch.object(od.time, "sleep"):
            code, output, errors = self.cli([self.url, "--dry-run", "--json"], api, self.env_credentials)
        self.assertEqual(code, 2)
        self.assertIn("offline", json_load(output)["error"])
        self.assertNotIn("Traceback", errors)

    def test_full_workflow_parses_http_and_resumes_video_then_downloads_subtitles(self):
        media_url = "https://media.example/movie.mp4?token=private"
        destination = self.root / "archive" / "The Martian (2015)" / "The Martian (2015).mp4"
        source = od.MediaSource("direct", media_url, "1080p", 6, True, etag='"v1"')
        self.partial(destination, source)
        requests = []

        def respond(request, **kwargs):
            requests.append(request)
            path = od.urlparse(request.full_url).path
            status, extra = 200, {}
            if path == "/api/v2/movies":
                data = od.json.dumps({"movies": [{"id": 1, "name": "The Martian", "year": "2015", "slug": "the-martian"}]}).encode()
            elif path == "/api/v2/movies/1":
                data = od.json.dumps({"download_url": media_url, "resolution": "1080p", "subtitles": [{"lang": "en", "url": "https://media.example/en.vtt"}]}).encode()
            elif path == "/movie.mp4":
                extra["ETag"] = '"v1"'
                if request.get_method() == "HEAD":
                    data = b"abcdef"
                else:
                    self.assertEqual(request.get_header("Range"), "bytes=3-")
                    self.assertEqual(request.get_header("If-range"), '"v1"')
                    data, status = b"def", 206
                    extra["Content-Range"] = "bytes 3-5/6"
            elif path == "/en.vtt":
                data = b"WEBVTT\n"
            else:
                self.fail(f"unexpected request: {request.full_url}")
            headers = {"Content-Length": str(len(data)), "Connection": "close", **extra}
            head = f"HTTP/1.1 {status} OK\r\n" + "".join(f"{key}: {value}\r\n" for key, value in headers.items()) + "\r\n"
            socket = mock.Mock()
            socket.makefile.return_value = io.BytesIO(head.encode() + (b"" if request.get_method() == "HEAD" else data))
            response = od.http.client.HTTPResponse(socket, method=request.get_method())
            response.begin()
            return response

        with mock.patch.object(od, "urlopen", side_effect=respond):
            code, output, _ = self.cli([self.url, "--json", "--subs", "en", "-o", str(self.root / "archive")], env=self.env_credentials)
        self.assertEqual(code, 0)
        result = json_load(output)
        self.assertTrue(result["ok"])
        self.assertEqual(result["summary"]["download_bytes"], 3)
        self.assertEqual(destination.read_bytes(), b"abcdef")
        self.assertEqual(destination.with_suffix(".en.vtt").read_bytes(), b"WEBVTT\n")
        self.assertEqual([request.get_method() for request in requests], ["GET", "GET", "HEAD", "GET", "GET"])
        self.assertFalse(self.credentials.exists())


class TransferTests(IsolatedTests):
    def setUp(self):
        super().setUp()
        self.destination = self.root / "movie.mp4"
        self.source = od.MediaSource("direct", "https://media.example/movie.mp4", "1080p", 6, True, etag='"v1"')

    def transfer(self, response, source=None):
        source = source or self.source
        with mock.patch.object(od, "request_url", return_value=response) as request, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            od.download_video(source.url, self.destination, source.size, "movie", source=source, progress=False)
        return request

    def test_resume_sends_if_range_and_removes_metadata_after_completion(self):
        partial = self.partial(self.destination, self.source)
        request = self.transfer(Response(b"def", 206, {"Content-Range": "bytes 3-5/6", "ETag": '"v1"'}))
        self.assertEqual(self.destination.read_bytes(), b"abcdef")
        request.assert_called_once_with(self.source.url, start=3, validator='"v1"')
        self.assertFalse(partial.exists())
        self.assertFalse(partial.with_suffix(".part.json").exists())

    def test_wrong_ranges_do_not_change_existing_partial(self):
        for content_range in ("bytes 0-2/6", "bytes 3-2/6", "garbage", "bytes 3-6/6"):
            with self.subTest(content_range=content_range):
                partial = self.partial(self.destination, self.source)
                with self.assertRaises(od.DownloadError):
                    self.transfer(Response(b"def", 206, {"Content-Range": content_range}))
                self.assertEqual(partial.read_bytes(), b"abc")
                self.assertFalse(self.destination.exists())

    def test_changed_remote_size_preserves_partial_and_requests_refresh(self):
        partial = self.partial(self.destination, self.source)
        with self.assertRaises(od.TransferError):
            self.transfer(Response(b"defg", 206, {"Content-Range": "bytes 3-6/7"}))
        self.assertEqual(partial.read_bytes(), b"abc")

    def test_range_with_unknown_total_can_use_the_probed_size(self):
        self.partial(self.destination, self.source)
        self.transfer(Response(b"def", 206, {"Content-Range": "bytes 3-5/*"}))
        self.assertEqual(self.destination.read_bytes(), b"abcdef")

    def test_ignored_range_restarts_using_full_response(self):
        self.partial(self.destination, self.source)
        self.transfer(Response(b"abcdef", 200, {"Content-Length": "6", "ETag": '"v1"'}))
        self.assertEqual(self.destination.read_bytes(), b"abcdef")

    def test_changed_validator_is_rejected_before_writing(self):
        partial = self.partial(self.destination, self.source)
        with self.assertRaises(od.TransferError):
            self.transfer(Response(b"xyz", 206, {"Content-Range": "bytes 3-5/6", "ETag": '"v2"'}))
        self.assertEqual(partial.read_bytes(), b"abc")

    def test_changed_source_restarts_instead_of_appending(self):
        self.partial(self.destination, self.source)
        changed = od.MediaSource("direct", self.source.url, "1080p", 6, True, etag='"v2"')
        request = self.transfer(Response(b"uvwxyz", headers={"Content-Length": "6", "ETag": '"v2"'}), changed)
        request.assert_called_once_with(changed.url, start=None)
        self.assertEqual(self.destination.read_bytes(), b"uvwxyz")

    def test_legacy_partial_without_metadata_restarts(self):
        self.destination.with_suffix(".mp4.part").write_bytes(b"abc")
        request = self.transfer(Response(b"abcdef", headers={"Content-Length": "6"}))
        request.assert_called_once_with(self.source.url, start=None)
        self.assertEqual(self.destination.read_bytes(), b"abcdef")

    def test_excess_bytes_are_not_saved_as_a_complete_file(self):
        partial = self.partial(self.destination, self.source)
        with self.assertRaises(od.DownloadError):
            self.transfer(Response(b"defg", 206, {"Content-Range": "bytes 3-5/6"}))
        self.assertEqual(partial.read_bytes(), b"abc")
        self.assertFalse(self.destination.exists())

    def test_completed_matching_partial_is_promoted_without_network(self):
        self.partial(self.destination, self.source, b"abcdef")
        request = self.transfer(Response())
        request.assert_not_called()
        self.assertEqual(self.destination.read_bytes(), b"abcdef")

    def test_interrupted_transfer_leaves_a_resumable_partial(self):
        class Interrupted(Response):
            def read(self, size=-1):
                if not self.tell():
                    return super().read(3)
                raise od.URLError("connection dropped")
        with self.assertRaises(od.URLError):
            self.transfer(Interrupted(b"abcdef", headers={"Content-Length": "6"}))
        self.assertEqual(self.destination.with_suffix(".mp4.part").read_bytes(), b"abc")
        self.transfer(Response(b"def", 206, {"Content-Range": "bytes 3-5/6"}))
        self.assertEqual(self.destination.read_bytes(), b"abcdef")

    def test_weak_etag_is_not_sent_as_if_range(self):
        source = od.MediaSource("direct", self.source.url, "1080p", 6, True, etag='W/"v1"')
        self.partial(self.destination, source)
        request = self.transfer(Response(b"def", 206, {"Content-Range": "bytes 3-5/6"}), source)
        request.assert_called_once_with(source.url, start=3)

    def test_http_request_contains_range_and_validator(self):
        with mock.patch.object(od, "urlopen", return_value=Response()) as opener:
            od.request_url(self.source.url, start=3, validator='"v1"')
        request = opener.call_args.args[0]
        self.assertEqual(request.get_header("Range"), "bytes=3-")
        self.assertEqual(request.get_header("If-range"), '"v1"')

    def test_rotating_signed_url_can_resume_when_validator_matches(self):
        self.partial(self.destination, self.source)
        rotated = od.MediaSource("direct", self.source.url + "?token=new", "1080p", 6, True, etag='"v1"')
        request = self.transfer(Response(b"def", 206, {"Content-Range": "bytes 3-5/6"}), rotated)
        self.assertEqual(request.call_args.kwargs["start"], 3)
        self.assertEqual(self.destination.read_bytes(), b"abcdef")


class PlanningTests(IsolatedTests):
    def setUp(self):
        super().setUp()
        self.api = mock.Mock()
        self.api.detail.return_value = {"subtitles": [{"lang": "en", "url": "https://media.example/en.vtt"}]}
        self.source = od.MediaSource("direct", "https://media.example/movie.mp4", "1080p", 10, True, etag='"v1"')

    def plan(self, target, **kwargs):
        with mock.patch.object(od, "select_source", return_value=self.source):
            return od.plan_target(self.api, target, {"en", "ru"}, None, **kwargs)

    def test_remaining_traffic_and_space_account_for_valid_partial(self):
        target = self.target()
        self.partial(target.video_path, self.source, b"x" * 9)
        plan = self.plan(target)
        self.assertEqual(plan.download_bytes, 1)
        self.assertEqual(plan.additional_space_bytes, 1)
        self.assertEqual(plan.resume_bytes, 9)
        self.assertIn("ru", " ".join(plan.warnings))
        self.assertEqual(od.plan_summary([plan])["download_bytes"], 1)

    def test_dry_run_creates_no_directories_or_locks(self):
        target = self.target()
        with mock.patch.object(od, "select_source", return_value=self.source):
            result = od.dry_run(self.api, [target], set(), None, json_output=True)
        self.assertEqual(result["summary"]["download_bytes"], 10)
        self.assertFalse(target.directory.exists())

    def test_upgrade_requires_room_for_replacement_while_old_file_is_preserved(self):
        target = self.target()
        target.directory.mkdir()
        target.video_path.write_bytes(b"old")
        replacement = target.directory / ".movie.1080p.upgrade.mp4"
        self.partial(replacement, self.source, b"xxxx")
        with mock.patch.object(od, "local_quality", return_value="720p"):
            plan = self.plan(target)
        self.assertEqual(plan.action, "upgrade")
        self.assertEqual(plan.download_bytes, 6)
        self.assertEqual(plan.additional_space_bytes, 6)
        self.assertEqual(target.video_path.read_bytes(), b"old")
        self.assertEqual(od.plan_summary([plan])["final_size_bytes"], 10)

    def test_finished_upgrade_left_before_rename_needs_no_more_disk_space(self):
        target = self.target()
        target.directory.mkdir()
        target.video_path.write_bytes(b"old")
        replacement = target.directory / ".movie.1080p.upgrade.mp4"
        replacement.write_bytes(b"x" * 10)
        with mock.patch.object(od, "local_quality", return_value="720p"):
            plan = self.plan(target)
        self.assertEqual(plan.download_bytes, 0)
        self.assertEqual(plan.additional_space_bytes, 0)
        self.assertEqual(plan.action, "upgrade")

    def test_restart_counts_full_traffic_but_reuses_partial_disk_space(self):
        target = self.target()
        self.partial(target.video_path, self.source, b"x" * 9)
        plan = self.plan(target, restart=True)
        self.assertEqual(plan.download_bytes, 10)
        self.assertEqual(plan.additional_space_bytes, 1)

    def test_hls_estimate_does_not_claim_resume_support(self):
        self.source = od.MediaSource("hls", "https://media.example/master", "720p", 10, False, 0)
        target = self.target()
        target.directory.mkdir()
        od.partial_path(target.video_path, "hls").write_bytes(b"xx")
        with mock.patch.object(od.shutil, "which", return_value="/usr/bin/ffmpeg"):
            plan = self.plan(target)
        self.assertEqual(plan.download_bytes, 10)
        self.assertEqual(plan.additional_space_bytes, 8)
        self.assertTrue(od.plan_summary([plan])["estimated"])

    def test_unknown_sizes_remain_unknown(self):
        self.source = od.MediaSource("direct", "https://media.example/video", "1080p", None, True)
        summary = od.plan_summary([self.plan(self.target())])
        self.assertIsNone(summary["download_bytes"])
        self.assertIsNone(summary["additional_space_bytes"])

    def test_sequential_upgrades_include_previous_growth_in_peak_space(self):
        plans = []
        for index, (old_size, new_size) in enumerate(((3, 10), (4, 12)), 1):
            target = self.target(index)
            target.directory.mkdir()
            target.video_path.write_bytes(b"x" * old_size)
            self.source = od.MediaSource("direct", "https://media.example/video", "1080p", new_size, True)
            with mock.patch.object(od, "local_quality", return_value="720p"):
                plans.append(self.plan(target))
        self.assertEqual(od.plan_summary(plans)["additional_space_bytes"], 19)

    def test_restart_discards_old_partial_once_but_resumes_a_failed_new_attempt(self):
        target = self.target()
        self.partial(target.video_path, self.source, b"old")
        attempts = []

        def download(url, destination, size, label, **kwargs):
            attempts.append(od.partial_path(destination).read_bytes())
            if len(attempts) == 1:
                self.partial(destination, self.source, b"new")
                raise od.URLError("connection dropped")
            destination.write_bytes(b"x" * size)

        with mock.patch.object(od, "select_source", return_value=self.source), mock.patch.object(od, "download_video", side_effect=download), mock.patch.object(od.time, "sleep"), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = od.download_all(self.api, [target], set(), 0, None, restart=True)
        self.assertEqual(attempts, [b"", b"new"])
        self.assertFalse(result["errors"])

    def test_lock_rejects_second_writer_and_can_be_reacquired(self):
        path = self.target().video_path
        with od.target_lock(path):
            with self.assertRaisesRegex(od.DownloadError, "another ororo process"):
                with od.target_lock(path):
                    self.fail("second lock acquired")
        with od.target_lock(path):
            pass

    def test_keep_going_downloads_next_target_after_individual_failure(self):
        targets = [self.target(1), self.target(2)]
        self.api.detail.side_effect = [od.DownloadError("unavailable"), {"subtitles": []}]
        with mock.patch.object(od, "select_source", return_value=self.source), mock.patch.object(od, "download_video") as download, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = od.download_all(self.api, targets, set(), 0, None, keep_going=True)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(download.call_args.args[1], targets[1].video_path)

    def test_disk_full_stops_batch_even_with_keep_going(self):
        targets = [self.target(1), self.target(2)]
        with mock.patch.object(od, "select_source", return_value=self.source), mock.patch.object(od.shutil, "disk_usage", return_value=mock.Mock(free=0)), mock.patch.object(od, "download_video") as download, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(od.FatalError, "not enough disk space"):
                od.download_all(self.api, targets, set(), 0, None, keep_going=True)
        self.assertEqual(self.api.detail.call_count, 1)
        download.assert_not_called()

    def test_auth_failure_stops_batch_even_with_keep_going(self):
        self.api.detail.side_effect = od.AuthenticationError("rejected")
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(od.AuthenticationError):
            od.download_all(self.api, [self.target(1), self.target(2)], set(), 0, None, keep_going=True)
        self.assertEqual(self.api.detail.call_count, 1)

    def test_retry_only_retries_transient_errors(self):
        for error, attempts in ((od.DownloadError("bad input"), 1), (od.HTTPError("https://example.test", 404, "missing", {}, None), 1), (od.HTTPError("https://example.test", 503, "busy", {}, None), 3)):
            with self.subTest(error=type(error).__name__), mock.patch.object(od.time, "sleep"):
                operation = mock.Mock(side_effect=error)
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(type(error)):
                    od.retry(operation, "test")
                self.assertEqual(operation.call_count, attempts)


class APITests(IsolatedTests):
    def test_first_failure_switches_address_without_retries_or_delay(self):
        failures = (
            TimeoutError("The read operation timed out"),
            od.URLError("offline"),
            od.HTTPError("https://example.test", 503, "busy", {}, None),
            od.HTTPError("https://example.test", 403, "blocked", {}, None),
        )
        for failure in failures:
            errors = io.StringIO()
            with self.subTest(error=str(failure)), mock.patch.object(
                od, "urlopen", side_effect=[failure, Response(b'{"movies": []}')]
            ) as opener, mock.patch.object(od.time, "sleep") as sleep, contextlib.redirect_stderr(errors):
                api = od.OroroAPI("demo", "secret")
                self.assertEqual(api.catalog("movies"), [])
                self.assertEqual(
                    [call.args[0].host for call in opener.call_args_list],
                    list(od.API_DOMAINS[:2]),
                )
                self.assertTrue(all(call.kwargs["timeout"] == 10 for call in opener.call_args_list))
                sleep.assert_not_called()
                self.assertIn(od.API_DOMAINS[0], errors.getvalue())
                self.assertIn(f"trying {od.API_DOMAINS[1]}", errors.getvalue())
                self.assertNotIn("secret", errors.getvalue())
                self.assertNotIn(api.authorization, errors.getvalue())

    def test_successful_address_is_reused_for_subsequent_requests(self):
        responses = [TimeoutError("offline"), Response(b'{"movies": []}'), Response(b'{"shows": []}')]
        with mock.patch.object(od, "urlopen", side_effect=responses) as opener, contextlib.redirect_stderr(io.StringIO()):
            api = od.OroroAPI("demo", "secret")
            self.assertEqual(api.catalog("movies"), [])
            self.assertEqual(api.catalog("shows"), [])
        self.assertEqual(
            [call.args[0].host for call in opener.call_args_list],
            [od.API_DOMAINS[0], od.API_DOMAINS[1], od.API_DOMAINS[1]],
        )

    def test_all_addresses_are_attempted_once_and_reported_on_failure(self):
        for preferred in (None, od.API_DOMAINS[2]):
            with self.subTest(preferred=preferred), mock.patch.object(
                od, "urlopen", side_effect=TimeoutError("offline")
            ) as opener, mock.patch.object(od.time, "sleep") as sleep, contextlib.redirect_stderr(io.StringIO()):
                api = od.OroroAPI("demo", "secret")
                api.active_domain = preferred
                with self.assertRaises(od.DownloadError) as raised:
                    api.catalog("movies")
                hosts = [call.args[0].host for call in opener.call_args_list]
                self.assertCountEqual(hosts, od.API_DOMAINS)
                self.assertEqual(hosts[0], preferred or od.API_DOMAINS[0])
                for domain in od.API_DOMAINS:
                    self.assertIn(f"{domain}: offline", str(raised.exception))
                sleep.assert_not_called()

    def test_auth_subscription_and_missing_item_errors_stop_immediately(self):
        for status, error_type in ((401, od.AuthenticationError), (402, od.FatalError), (404, od.DownloadError)):
            body = io.BytesIO(b"error")
            failure = od.HTTPError("https://example.test", status, "rejected", {}, body)
            with self.subTest(status=status), mock.patch.object(od, "urlopen", side_effect=failure) as opener:
                with self.assertRaises(error_type):
                    od.OroroAPI("demo", "secret").catalog("movies")
                opener.assert_called_once()
                self.assertTrue(body.closed)

    def test_read_timeout_closes_response_and_tries_next_address(self):
        response = Response()
        response.read = mock.Mock(side_effect=TimeoutError("The read operation timed out"))
        with mock.patch.object(od, "urlopen", side_effect=[response, Response(b'{"movies": []}')]) as opener, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(od.OroroAPI("demo", "secret").catalog("movies"), [])
        self.assertTrue(response.closed)
        self.assertEqual(opener.call_count, 2)
        self.assertEqual(opener.call_args.args[0].host, od.API_DOMAINS[1])

    def test_invalid_json_response_falls_back_to_another_address(self):
        for body in (b"<html>Unavailable</html>", b"[]"):
            with self.subTest(body=body), mock.patch.object(
                od, "urlopen", side_effect=[Response(body), Response(b'{"movies": []}')]
            ) as opener, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(od.OroroAPI("demo", "secret").catalog("movies"), [])
                self.assertEqual(opener.call_count, 2)


def json_load(value):
    return od.json.loads(value)


if __name__ == "__main__":
    unittest.main()
