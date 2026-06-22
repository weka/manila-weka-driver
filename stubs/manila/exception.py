# Stub — provides minimal manila.exception for standalone testing.
# In production, the real Manila package supplies this module.


class ManilaException(Exception):
    """Base Manila exception."""
    message = "An unknown exception occurred."

    def __init__(self, message=None, **kwargs):
        self.kwargs = kwargs
        if message is None:
            message = self.message % kwargs if kwargs else self.message
        super(ManilaException, self).__init__(message)


class InvalidInput(ManilaException):
    message = "Invalid input received: %(reason)s"


class InvalidShare(ManilaException):
    message = "Invalid share: %(reason)s"


class InvalidShareAccess(ManilaException):
    message = "Invalid share access rule: %(reason)s"


class InvalidShareAccessLevel(ManilaException):
    message = "Invalid or unsupported share access level: %(level)s"


class ShareNotFound(ManilaException):
    message = "Share %(share_id)s could not be found."

    def __init__(self, share_id=None, **kwargs):
        kwargs['share_id'] = share_id or 'unknown'
        super(ShareNotFound, self).__init__(**kwargs)


class ShareShrinkingPossibleDataLoss(ManilaException):
    message = "Share %(share_id)s shrinking error due to possible data loss."

    def __init__(self, share_id=None, **kwargs):
        kwargs['share_id'] = share_id or 'unknown'
        super(ShareShrinkingPossibleDataLoss, self).__init__(**kwargs)


class SnapshotNotFound(ManilaException):
    message = "Snapshot %(snapshot_id)s could not be found."

    def __init__(self, snapshot_id=None, **kwargs):
        kwargs['snapshot_id'] = snapshot_id or 'unknown'
        super(SnapshotNotFound, self).__init__(**kwargs)


class ShareSnapshotNotFound(ManilaException):
    message = "Snapshot %(snapshot_id)s could not be found."

    def __init__(self, snapshot_id=None, **kwargs):
        kwargs['snapshot_id'] = snapshot_id or 'unknown'
        super(ShareSnapshotNotFound, self).__init__(**kwargs)


class ManageInvalidShare(ManilaException):
    message = "Manage existing share failed: %(reason)s"


class ManageExistingShareTypeMismatch(ManilaException):
    message = ("Manage existing share failed due to type mismatch: "
               "%(reason)s")


class UnmanageInvalidShare(ManilaException):
    message = "Unmanage share failed: %(reason)s"


class ShareBackendException(ManilaException):
    message = "Share backend error: %(msg)s"
