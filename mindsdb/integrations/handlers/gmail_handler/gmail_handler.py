import json
import re

import requests

from mindsdb.integrations.libs.response import (
    HandlerStatusResponse as StatusResponse,
    HandlerResponse as Response,
    RESPONSE_TYPE
)
from bs4 import BeautifulSoup
from mindsdb.integrations.utilities.sql_utils import extract_comparison_conditions
from mindsdb.integrations.libs.api_handler import APIHandler, APITable
from mindsdb_sql.parser import ast
from mindsdb.utilities import log
from mindsdb_sql import parse_sql
import os
import time
from typing import List
import pandas as pd
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from email.message import EmailMessage

from base64 import urlsafe_b64encode, urlsafe_b64decode

DEFAULT_SCOPES = ['https://www.googleapis.com/auth/gmail.compose',
                  'https://www.googleapis.com/auth/gmail.readonly']


class EmailsTable(APITable):
    """Implementation for the emails table for Gmail"""

    def select(self, query: ast.Select) -> pd.DataFrame:
        """Pulls emails from Gmail "users.messages.list" API

        Parameters
        ----------
        query : ast.Select
           Given SQL SELECT query

        Returns
        -------
        pd.DataFrame
            Email matching the query

        Raises
        ------
        NotImplementedError
            If the query contains an unsupported operation or condition
        """

        conditions = extract_comparison_conditions(query.where)

        params = {}
        for op, arg1, arg2 in conditions:

            if op == 'or':
                raise NotImplementedError(f'OR is not supported')

            if arg1 in ['query', 'label_ids', 'include_spam_trash']:
                if op == '=':
                    if arg1 == 'query':
                        params['q'] = arg2
                    elif arg1 == 'label_ids':
                        params['labelIds'] = arg2.split(',')
                    else:
                        params['includeSpamTrash'] = arg2
                else:
                    raise NotImplementedError(f'Unknown op: {op}')

            else:
                raise NotImplementedError(f'Unknown clause: {arg1}')

        if query.limit is not None:
            params['maxResults'] = query.limit.value

        result = self.handler.call_gmail_api(
            method_name='list_messages',
            params=params
        )

        # filter targets
        columns = []
        for target in query.targets:
            if isinstance(target, ast.Star):
                columns = self.get_columns()
                break
            elif isinstance(target, ast.Identifier):
                columns.append(target.parts[-1])
            else:
                raise NotImplementedError(f"Unknown query target {type(target)}")

        # columns to lower case
        columns = [name.lower() for name in columns]

        if len(result) == 0:
            return pd.DataFrame([], columns=columns)

        # add absent columns
        for col in set(columns) & set(result.columns) ^ set(columns):
            result[col] = None

        # filter by columns
        result = result[columns]
        # Rename columns
        for target in query.targets:
            if target.alias:
                result.rename(columns={target.parts[-1]: str(target.alias)}, inplace=True)
        return result

    def get_columns(self) -> List[str]:
        """Gets all columns to be returned in pandas DataFrame responses

        Returns
        -------
        List[str]
            List of columns
        """
        return [
            'id',
            'message_id',
            'thread_id',
            'label_ids',
            'sender',
            'to',
            'date',
            'subject',
            'snippet',
            'body',
            'attachments',
        ]

    def insert(self, query: ast.Insert):
        """Sends reply emails using the Gmail "users.messages.send" API

        Parameters
        ----------
        query : ast.Insert
           Given SQL INSERT query

        Raises
        ------
        ValueError
            If the query contains an unsupported condition
        """
        columns = [col.name for col in query.columns]

        if not 'credentials_file' in self.handler.connection_args:
            raise ValueError(
                "Need the Google Auth Credentials file in order to write an email"
            )

        supported_columns = {"message_id", "thread_id", "to_email", "subject", "body"}
        if not set(columns).issubset(supported_columns):
            unsupported_columns = set(columns).difference(supported_columns)
            raise ValueError(
                "Unsupported columns for create email: "
                + ", ".join(unsupported_columns)
            )

        for row in query.values:
            params = dict(zip(columns, row))

            if not 'to_email' in params:
                raise ValueError('"to_email" parameter is required to send an email')

            message = EmailMessage()
            message['To'] = params['to_email']
            message['Subject'] = params['subject'] if 'subject' in params else ''

            content = params['body'] if 'body' in params else ''
            message.set_content(content)

            # If threadId is present then add References and In-Reply-To headers
            # so that proper threading can happen
            if 'thread_id' in params and 'message_id' in params:
                message['In-Reply-To'] = params['message_id']
                message['References'] = params['message_id']

            encoded_message = urlsafe_b64encode(message.as_bytes()).decode()

            message = {
                'raw': encoded_message
            }

            if 'thread_id' in params:
                message['threadId'] = params['thread_id']

            self.handler.call_gmail_api('send_message', {'body': message})


class GmailHandler(APIHandler):
    """A class for handling connections and interactions with the Gmail API.

    Attributes:
        credentials_file (str): The path to the Google Auth Credentials file for authentication
        and interacting with the Gmail API on behalf of the uesr.

        scopes (List[str], Optional): The scopes to use when authenticating with the Gmail API.
    """

    def __init__(self, name=None, **kwargs):
        super().__init__(name)
        self.connection_args = kwargs.get('connection_data', {})
        if 'credentials_file' in self.connection_args:
            self.credentials_file = self.connection_args['credentials_file']
        if 's3_credentials_file' in self.connection_args:
            self.s3_credentials_file = self.connection_args['s3_credentials_file']
        if not 'credentials_file' in self.connection_args and not 's3_credentials_file' in self.connection_args:
            raise ValueError(
                "Need the Google Auth Credentials file in order to connect to Gmail"
            )
        self.scopes = self.connection_args.get('scopes', DEFAULT_SCOPES)
        self.token_file = None
        self.max_page_size = 500
        self.max_batch_size = 100
        self.service = None
        self.is_connected = False

        emails = EmailsTable(self)
        self.emails = emails
        self._register_table('emails', emails)

    def create_connection(self) -> object:
        creds = None
        if self.s3_credentials_file:
            response = requests.get(self.s3_credentials_file)
            if response.status_code == 200:
                # data successfully retrieved
                data = response.content
                data = json.loads(data)
                flow = InstalledAppFlow.from_client_config(data, self.scopes)
                creds = flow.run_local_server(port=0, timeout_seconds=120)
            else:
                # handle errors
                print("Failed to retrieve data: ", response.status_code)

        else:
            token_file = os.path.join(os.path.dirname(self.credentials_file), 'token.json')

            if os.path.isfile(token_file):
                creds = Credentials.from_authorized_user_file(token_file, self.scopes)

            if not creds or not creds.valid:
                if creds and creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                elif not os.path.isfile(self.credentials_file):
                    raise Exception('Credentials must be a file path')
                else:
                    flow = InstalledAppFlow.from_client_secrets_file(self.credentials_file, self.scopes)
                    creds = flow.run_local_server(port=0, timeout_seconds=120)

            # Save the credentials for the next run
            with open(token_file, 'w') as token:
                token.write(creds.to_json())

        return build('gmail', 'v1', credentials=creds)

    def connect(self) -> object:
        """Authenticate with the Gmail API using the credentials file.

        Returns
        -------
        service: object
            The authenticated Gmail API service object.
        """
        if self.is_connected and self.service is not None:
            return self.service
        try:
            self.service = self.create_connection()
        except Exception as e:
            raise Exception(f'Error connecting to Gmail API: {e}')
        self.is_connected = True
        return self.service

    def check_connection(self) -> StatusResponse:
        """Check connection to the handler.

        Returns
        -------
        StatusResponse
            Status confirmation
        """
        response = StatusResponse(False)

        try:
            # Call the Gmail API
            service = self.connect()

            result = service.users().getProfile(userId='me').execute()

            if result and result.get('emailAddress', None) is not None:
                response.success = True
        except HttpError as error:
            response.error_message = f'Error connecting to Gmail api: {error}.'
            log.logger.error(response.error_message)

        if response.success is False and self.is_connected is True:
            self.is_connected = False

        return response

    def native_query(self, query_string: str = None) -> Response:
        ast = parse_sql(query_string, dialect="mindsdb")

        return self.query(ast)

    def _get_attachments(self, parts, message_id):
        if not parts:
            return []
        attachments = []
        for part in parts:
            if part['filename']:
                attachment = {
                    'message_id': message_id,
                    'filename': part['filename'],
                    'mimeType': part['mimeType'],
                    'size': part['body']['size'],
                    'attachment_id': part['body']['attachmentId']
                }
                attachment_json = json.dumps(attachment)
                attachments.append(attachment_json)
            elif 'parts' in part:
                attachments += self._get_attachments(part['parts'], message_id)
        return attachments

    def _parse_parts(self, parts):
        if not parts:
            return

        body = ''
        for part in parts:
            if part['mimeType'] == 'text/plain':
                part_body = part.get('body', {}).get('data', '')
                body += urlsafe_b64decode(part_body).decode('utf-8')
            elif part['mimeType'] == 'multipart/alternative' or 'parts' in part:
                # Recursively iterate over nested parts to find the plain text body
                body += self._parse_parts(part['parts'])
            elif part['mimeType'] == 'text/html':
                body += self.extract_html_body(part['body']['data'])
            else:
                log.logger.debug(f"Unhandled mimeType: {part['mimeType']}")
        body = re.sub(r'(?<!>)\s+(?!<)', ' ', body).strip()
        return body

    def extract_html_body(self, encoded_body):
        """Extracts the HTML body from the encoded body.
            Args:
                encoded_body (str): The encoded body.
            Returns:
                str: The HTML body.
        """
        html_message = urlsafe_b64decode(encoded_body).decode('utf-8')
        soup = BeautifulSoup(html_message, 'html.parser')
        # Extract the text from the HTML
        for element in soup(['style', 'script']):
            element.extract()

        # Extract the visible text from the HTML and remove whitespace characters
        text = soup.get_text().strip()
        return text

    def _parse_message(self, data, message, exception):
        if exception:
            log.logger.error(f'Exception in getting full email: {exception}')
            return

        payload = message['payload']
        headers = payload.get("headers", [])
        parts = payload.get("parts")

        row = {
            'id': message['id'],
            'thread_id': message['threadId'],
            'label_ids': message.get('labelIds', []),
            'snippet': message.get('snippet', ''),
        }

        for header in headers:
            key = header['name'].lower()
            value = header['value']

            if key in ['to', 'subject', 'date']:
                row[key] = value
            elif key == 'from':
                row['sender'] = value
            elif key == 'message-id':
                row['message_id'] = value

        row['body'] = self._parse_parts(parts)
        row['attachments'] = self._get_attachments(parts, message['id'])
        data.append(row)

    def _get_messages(self, data, messages):
        batch_req = self.service.new_batch_http_request(
            lambda id, response, exception: self._parse_message(data, response, exception))
        for message in messages:
            batch_req.add(self.service.users().messages().get(userId='me', id=message['id']))

        batch_req.execute()

    def _handle_list_messages_response(self, data, messages):
        total_pages = len(messages) // self.max_batch_size
        for page in range(total_pages):
            self._get_messages(data, messages[page * self.max_batch_size:(page + 1) * self.max_batch_size])

        # Get the remaining messsages, if any
        if len(messages) % self.max_batch_size > 0:
            self._get_messages(data, messages[total_pages * self.max_batch_size:])

    def call_gmail_api(self, method_name: str = None, params: dict = None) -> pd.DataFrame:
        """Call Gmail API and map the data to pandas DataFrame
        Args:
            method_name (str): method name
            params (dict): query parameters
        Returns:
            DataFrame
        """
        service = self.connect()
        if method_name == 'list_messages':
            method = service.users().messages().list
        elif method_name == 'send_message':
            method = service.users().messages().send
        else:
            raise NotImplementedError(f'Unknown method_name: {method_name}')

        left = None
        count_results = None
        if 'maxResults' in params:
            count_results = params['maxResults']

        params['userId'] = 'me'

        data = []
        limit_exec_time = time.time() + 60

        while True:
            if time.time() > limit_exec_time:
                raise RuntimeError('Handler request timeout error')

            if count_results is not None:
                left = count_results - len(data)
                if left == 0:
                    break
                elif left < 0:
                    # got more results that we need
                    data = data[:left]
                    break

                if left > self.max_page_size:
                    params['maxResults'] = self.max_page_size
                else:
                    params['maxResults'] = left

            log.logger.debug(f'Calling Gmail API: {method_name} with params ({params})')

            resp = method(**params).execute()

            if 'messages' in resp:
                self._handle_list_messages_response(data, resp['messages'])
            elif isinstance(resp, dict):
                data.append(resp)

            if count_results is not None and 'nextPageToken' in resp:
                params['pageToken'] = resp['nextPageToken']
            else:
                break

        df = pd.DataFrame(data)

        return df
