"""Cooperative cancellation around synchronous, potentially blocking iterators."""


def until_cancelled(source, cancelled):
    iterator = iter(source)
    while cancelled is None or not cancelled.is_set():
        try:
            item = next(iterator)
        except StopIteration:
            return
        yield item
