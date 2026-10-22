from src.adapter.minecraft.epoch_acl import Ace, WindowsAclVerifier, assess_descriptor

OWNER = b"owner-sid"
OTHER = b"other-sid"
WRITE = 0x2
GENERIC_ALL = 0x10000000
GENERIC_WRITE = 0x40000000


def test_descriptor_accepts_owner_write_and_other_read_only():
    result = assess_descriptor(OWNER, OWNER, (Ace(0, 0, WRITE, OWNER), Ace(0, 0, 1, OTHER)))
    assert result.verified


def test_descriptor_rejects_null_dacl_wrong_owner_unknown_and_broad_write():
    assert not assess_descriptor(OWNER, OWNER, None).verified
    assert not assess_descriptor(OTHER, OWNER, ()).verified
    assert not assess_descriptor(OWNER, OWNER, (Ace(7, 0, 0, OWNER),)).verified
    assert not assess_descriptor(OWNER, OWNER, (Ace(0, 0, WRITE, OTHER),)).verified
    assert not assess_descriptor(OWNER, OWNER, (Ace(0, 0, GENERIC_ALL, OTHER),)).verified
    assert not assess_descriptor(OWNER, OWNER, (Ace(0, 0, GENERIC_WRITE, OTHER),)).verified


def test_descriptor_allows_service_full_control_when_not_inherited():
    assert assess_descriptor(OWNER, OWNER, (Ace(0, 0, GENERIC_ALL, OWNER),)).verified


def test_descriptor_rejects_inherited_write_but_allows_readonly_other_principal():
    assert not assess_descriptor(OWNER, OWNER, (Ace(0, 0x10, WRITE, OWNER),)).verified
    assert assess_descriptor(OWNER, OWNER, (Ace(1, 0, WRITE, OTHER), Ace(0, 0, 1, OTHER))).verified


def test_native_verifier_is_read_only_and_fail_closed_off_windows(tmp_path):
    result = WindowsAclVerifier().assess_path(tmp_path)
    assert not result.verified
