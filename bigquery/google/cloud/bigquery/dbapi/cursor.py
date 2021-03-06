# Copyright 2017 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

"""Cursor for the Google BigQuery DB-API."""

import collections
import uuid

import six

from google.cloud.bigquery.dbapi import _helpers
from google.cloud.bigquery.dbapi import exceptions


# Per PEP 249: A 7-item sequence containing information describing one result
# column. The first two items (name and type_code) are mandatory, the other
# five are optional and are set to None if no meaningful values can be
# provided.
Column = collections.namedtuple(
    'Column',
    [
        'name', 'type_code', 'display_size', 'internal_size', 'precision',
        'scale', 'null_ok',
    ])


class Cursor(object):
    """DB-API Cursor to Google BigQuery.

    :type connection: :class:`~google.cloud.bigquery.dbapi.Connection`
    :param connection: A DB-API connection to Google BigQuery.
    """
    def __init__(self, connection):
        self.connection = connection
        self.description = None
        # Per PEP 249: The attribute is -1 in case no .execute*() has been
        # performed on the cursor or the rowcount of the last operation
        # cannot be determined by the interface.
        self.rowcount = -1
        # Per PEP 249: The arraysize attribute defaults to 1, meaning to fetch
        # a single row at a time.
        self.arraysize = 1
        self._query_data = None
        self._page_token = None
        self._has_fetched_all_rows = True

    def close(self):
        """No-op."""

    def _set_description(self, schema):
        """Set description from schema.

        :type schema: Sequence[google.cloud.bigquery.schema.SchemaField]
        :param schema: A description of fields in the schema.
        """
        if schema is None:
            self.description = None
            return

        self.description = tuple([
            Column(
                name=field.name,
                type_code=field.field_type,
                display_size=None,
                internal_size=None,
                precision=None,
                scale=None,
                null_ok=field.is_nullable)
            for field in schema])

    def _set_rowcount(self, query_results):
        """Set the rowcount from query results.

        Normally, this sets rowcount to the number of rows returned by the
        query, but if it was a DML statement, it sets rowcount to the number
        of modified rows.

        :type query_results:
            :class:`~google.cloud.bigquery.query.QueryResults`
        :param query_results: results of a query
        """
        total_rows = 0
        num_dml_affected_rows = query_results.num_dml_affected_rows

        if (query_results.total_rows is not None
                and query_results.total_rows > 0):
            total_rows = query_results.total_rows
        if num_dml_affected_rows is not None and num_dml_affected_rows > 0:
            total_rows = num_dml_affected_rows
        self.rowcount = total_rows

    def execute(self, operation, parameters=None):
        """Prepare and execute a database operation.

        .. note::
            When setting query parameters, values which are "text"
            (``unicode`` in Python2, ``str`` in Python3) will use
            the 'STRING' BigQuery type. Values which are "bytes" (``str`` in
            Python2, ``bytes`` in Python3), will use using the 'BYTES' type.

            A `~datetime.datetime` parameter without timezone information uses
            the 'DATETIME' BigQuery type (example: Global Pi Day Celebration
            March 14, 2017 at 1:59pm). A `~datetime.datetime` parameter with
            timezone information uses the 'TIMESTAMP' BigQuery type (example:
            a wedding on April 29, 2011 at 11am, British Summer Time).

            For more information about BigQuery data types, see:
            https://cloud.google.com/bigquery/docs/reference/standard-sql/data-types

            ``STRUCT``/``RECORD`` and ``REPEATED`` query parameters are not
            yet supported. See:
            https://github.com/GoogleCloudPlatform/google-cloud-python/issues/3524

        :type operation: str
        :param operation: A Google BigQuery query string.

        :type parameters: Mapping[str, Any] or Sequence[Any]
        :param parameters:
            (Optional) dictionary or sequence of parameter values.
        """
        self._query_results = None
        self._page_token = None
        self._has_fetched_all_rows = False
        client = self.connection._client
        job_id = str(uuid.uuid4())

        # The DB-API uses the pyformat formatting, since the way BigQuery does
        # query parameters was not one of the standard options. Convert both
        # the query and the parameters to the format expected by the client
        # libraries.
        formatted_operation = _format_operation(
            operation, parameters=parameters)
        query_parameters = _helpers.to_query_parameters(parameters)

        query_job = client.run_async_query(
            job_id,
            formatted_operation,
            query_parameters=query_parameters)
        query_job.use_legacy_sql = False
        query_job.begin()
        _helpers.wait_for_job(query_job)
        query_results = query_job.results()

        # Force the iterator to run because the query_results doesn't
        # have the total_rows populated. See:
        # https://github.com/GoogleCloudPlatform/google-cloud-python/issues/3506
        query_iterator = query_results.fetch_data()
        try:
            six.next(iter(query_iterator))
        except StopIteration:
            pass

        self._query_data = iter(
            query_results.fetch_data(max_results=self.arraysize))
        self._set_rowcount(query_results)
        self._set_description(query_results.schema)

    def executemany(self, operation, seq_of_parameters):
        """Prepare and execute a database operation multiple times.

        :type operation: str
        :param operation: A Google BigQuery query string.

        :type seq_of_parameters: Sequence[Mapping[str, Any] or Sequence[Any]]
        :param parameters: Sequence of many sets of parameter values.
        """
        for parameters in seq_of_parameters:
            self.execute(operation, parameters)

    def fetchone(self):
        """Fetch a single row from the results of the last ``execute*()`` call.

        :rtype: tuple
        :returns:
            A tuple representing a row or ``None`` if no more data is
            available.
        :raises: :class:`~google.cloud.bigquery.dbapi.InterfaceError`
            if called before ``execute()``.
        """
        if self._query_data is None:
            raise exceptions.InterfaceError(
                'No query results: execute() must be called before fetch.')

        try:
            return six.next(self._query_data)
        except StopIteration:
            return None

    def fetchmany(self, size=None):
        """Fetch multiple results from the last ``execute*()`` call.

        .. note::
            The size parameter is not used for the request/response size.
            Set the ``arraysize`` attribute before calling ``execute()`` to
            set the batch size.

        :type size: int
        :param size:
            (Optional) Maximum number of rows to return. Defaults to the
            ``arraysize`` property value.

        :rtype: List[tuple]
        :returns: A list of rows.
        :raises: :class:`~google.cloud.bigquery.dbapi.InterfaceError`
            if called before ``execute()``.
        """
        if self._query_data is None:
            raise exceptions.InterfaceError(
                'No query results: execute() must be called before fetch.')
        if size is None:
            size = self.arraysize

        rows = []
        for row in self._query_data:
            rows.append(row)
            if len(rows) >= size:
                break
        return rows

    def fetchall(self):
        """Fetch all remaining results from the last ``execute*()`` call.

        :rtype: List[tuple]
        :returns: A list of all the rows in the results.
        :raises: :class:`~google.cloud.bigquery.dbapi.InterfaceError`
            if called before ``execute()``.
        """
        if self._query_data is None:
            raise exceptions.InterfaceError(
                'No query results: execute() must be called before fetch.')
        return [row for row in self._query_data]

    def setinputsizes(self, sizes):
        """No-op."""

    def setoutputsize(self, size, column=None):
        """No-op."""


def _format_operation_list(operation, parameters):
    """Formats parameters in operation in the way BigQuery expects.

    The input operation will be a query like ``SELECT %s`` and the output
    will be a query like ``SELECT ?``.

    :type operation: str
    :param operation: A Google BigQuery query string.

    :type parameters: Sequence[Any]
    :param parameters: Sequence of parameter values.

    :rtype: str
    :returns: A formatted query string.
    :raises: :class:`~google.cloud.bigquery.dbapi.ProgrammingError`
        if a parameter used in the operation is not found in the
        ``parameters`` argument.
    """
    formatted_params = ['?' for _ in parameters]

    try:
        return operation % tuple(formatted_params)
    except TypeError as exc:
        raise exceptions.ProgrammingError(exc)


def _format_operation_dict(operation, parameters):
    """Formats parameters in operation in the way BigQuery expects.

    The input operation will be a query like ``SELECT %(namedparam)s`` and
    the output will be a query like ``SELECT @namedparam``.

    :type operation: str
    :param operation: A Google BigQuery query string.

    :type parameters: Mapping[str, Any]
    :param parameters: Dictionary of parameter values.

    :rtype: str
    :returns: A formatted query string.
    :raises: :class:`~google.cloud.bigquery.dbapi.ProgrammingError`
        if a parameter used in the operation is not found in the
        ``parameters`` argument.
    """
    formatted_params = {}
    for name in parameters:
        escaped_name = name.replace('`', r'\`')
        formatted_params[name] = '@`{}`'.format(escaped_name)

    try:
        return operation % formatted_params
    except KeyError as exc:
        raise exceptions.ProgrammingError(exc)


def _format_operation(operation, parameters=None):
    """Formats parameters in operation in way BigQuery expects.

    :type: str
    :param operation: A Google BigQuery query string.

    :type: Mapping[str, Any] or Sequence[Any]
    :param parameters: Optional parameter values.

    :rtype: str
    :returns: A formatted query string.
    :raises: :class:`~google.cloud.bigquery.dbapi.ProgrammingError`
        if a parameter used in the operation is not found in the
        ``parameters`` argument.
    """
    if parameters is None:
        return operation

    if isinstance(parameters, collections.Mapping):
        return _format_operation_dict(operation, parameters)

    return _format_operation_list(operation, parameters)
