import os, sys
sys.path.append(os.path.dirname(os.path.dirname(__file__)))
import bus_bot as bb


def test_is_tago_node_id():
    assert bb.is_tago_node_id("ICB164000104")
    assert not bb.is_tago_node_id("서울역")
    assert not bb.is_tago_node_id("ICB12")


def test_paginate():
    items = list(range(25))
    assert bb.paginate(items, 0, 10) == list(range(10))
    assert bb.paginate(items, 2, 10) == list(range(20, 25))


def test_normalize_arrmsg():
    assert bb._normalize_arrmsg("", 30) == ("곧 도착", "1정거장")
    assert bb._normalize_arrmsg("", 180) == ("3분", "1정거장")
