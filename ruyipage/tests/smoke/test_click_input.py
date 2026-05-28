# -*- coding: utf-8 -*-

import pytest


@pytest.mark.smoke
def test_click_and_input_on_fixture_page(page, fixture_page_url):
    page.get(fixture_page_url("basic_form.html"))

    page.ele("#click-btn").click_self()
    assert page.ele("#click-result").text == "clicked"

    input_ele = page.ele("#text-input")
    input_ele.input("hello smoke")
    page.ele("#mirror-btn").click_self()
    assert input_ele.value == "hello smoke"
    assert page.ele("#mirror").text == "hello smoke"
