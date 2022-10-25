import json
import datetime
from typing import Optional
from pathlib import Path

import pandas as pd
from mindsdb_sql.parser.dialects.mindsdb import (
    CreateDatasource,
    RetrainPredictor,
    CreatePredictor,
    DropDatasource,
    DropPredictor,
    CreateView
)
from mindsdb_sql import parse_sql
from mindsdb_sql.parser.dialects.mysql import Variable
from mindsdb_sql.parser.ast import (
    RollbackTransaction,
    CommitTransaction,
    StartTransaction,
    BinaryOperation,
    DropDatabase,
    NullConstant,
    Describe,
    Constant,
    Function,
    Explain,
    Delete,
    Insert,
    Select,
    Star,
    Show,
    Set,
    Use,
    Alter,
    Update,
    CreateTable,
    TableColumn,
    Identifier,
    DropTables,
    Operation,
    ASTNode,
    DropView,
    Union,
)
from mindsdb_sql import parse_sql
from mindsdb_sql.render.sqlalchemy_render import SqlalchemyRender
from mindsdb_sql.parser.ast import Identifier
from mindsdb_sql.planner.utils import query_traversal

from mindsdb.api.mysql.mysql_proxy.utilities.sql import query_df
from mindsdb.api.mysql.mysql_proxy.utilities import log
from mindsdb.api.mysql.mysql_proxy.utilities import (
    SqlApiException,
    ErBadDbError,
    ErBadTableError,
    ErKeyColumnDoesNotExist,
    ErTableExistError,
    ErDubFieldName,
    ErDbDropDelete,
    ErNonInsertableTable,
    ErNotSupportedYet,
    ErSqlSyntaxError,
    ErSqlWrongArguments,
)
from mindsdb.api.mysql.mysql_proxy.utilities.functions import get_column_in_case, download_file
from mindsdb.api.mysql.mysql_proxy.classes.sql_query import (
    SQLQuery, Column
)
from mindsdb.api.mysql.mysql_proxy.libs.constants.response_type import RESPONSE_TYPE
from mindsdb.api.mysql.mysql_proxy.libs.constants.mysql import (
    CHARSET_NUMBERS,
    ERR,
    TYPES,
    SERVER_VARIABLES,
)
from mindsdb.api.mysql.mysql_proxy.executor.data_types import ExecuteAnswer, ANSWER_TYPE
from mindsdb.integrations.libs.response import HandlerStatusResponse
from mindsdb.integrations.libs.const import HANDLER_CONNECTION_ARG_TYPE
from mindsdb.interfaces.model.functions import (
    get_model_record,
    get_model_records,
    get_predictor_integration
)
from mindsdb.integrations.libs.const import PREDICTOR_STATUS


class ExecuteCommands:

    def __init__(self, session, executor):
        self.session = session
        self.executor = executor

        self.charset_text_type = CHARSET_NUMBERS['utf8_general_ci']
        self.datahub = session.datahub

    def execute_command(self, statement):
        sql = None
        if self.executor is None:
            if isinstance(statement, ASTNode):
                sql = statement.to_string()
            sql_lower = sql.lower()
        else:
            sql = self.executor.sql
            sql_lower = self.executor.sql_lower

        if type(statement) == CreateDatasource:
            return self.answer_create_database(statement)
        if type(statement) == DropPredictor:
            database_name = self.session.database
            if len(statement.name.parts) > 1:
                database_name = statement.name.parts[0].lower()
            model_name = statement.name.parts[-1]

            try:
                self.session.model_controller.delete_model(model_name, project_name=database_name)
            except Exception as e:
                if not statement.if_exists:
                    raise e
            return ExecuteAnswer(ANSWER_TYPE.OK)
        elif type(statement) == DropTables:
            return self.answer_drop_tables(statement)
        elif type(statement) == DropDatasource or type(statement) == DropDatabase:
            return self.answer_drop_database(statement)
        elif type(statement) == Describe:
            # NOTE in sql 'describe table' is same as 'show columns'
            predictor_attrs = ("model", "features", "ensemble")
            if statement.value.parts[-1] in predictor_attrs:
                return self.answer_describe_predictor(statement.value.parts[-2:])
            else:
                return self.answer_describe_predictor(statement.value.parts[-1])
        elif type(statement) == RetrainPredictor:
            return self.answer_retrain_predictor(statement)
        elif type(statement) == Show:
            sql_category = statement.category.lower()
            if hasattr(statement, 'modes'):
                if isinstance(statement.modes, list) is False:
                    statement.modes = []
                statement.modes = [x.upper() for x in statement.modes]
            if sql_category in ('predictors', 'models'):
                where = BinaryOperation('=', args=[Constant(1), Constant(1)])
                if statement.from_table is not None:
                    where = BinaryOperation('and', args=[
                        where,
                        BinaryOperation('=', args=[
                            Identifier('project'),
                            Constant(statement.from_table.parts[-1])
                        ])
                    ])
                if statement.like is not None:
                    like = BinaryOperation('like', args=[Identifier('name'), Constant(statement.like)])
                    where = BinaryOperation('and', args=[where, like])
                if statement.where is not None:
                    where = BinaryOperation('and', args=[statement.where, where])

                new_statement = Select(
                    targets=[Star()],
                    from_table=Identifier(parts=['information_schema', 'models']),
                    where=where
                )
                query = SQLQuery(
                    new_statement,
                    session=self.session
                )
                return self.answer_select(query)
            elif sql_category == 'views':
                where = BinaryOperation('and', args=[
                    BinaryOperation('=', args=[Identifier('table_schema'), Constant('views')]),
                    BinaryOperation('like', args=[Identifier('table_type'), Constant('BASE TABLE')])
                ])
                if statement.where is not None:
                    where = BinaryOperation('and', args=[where, statement.where])
                if statement.like is not None:
                    like = BinaryOperation('like', args=[Identifier('View'), Constant(statement.like)])
                    where = BinaryOperation('and', args=[where, like])

                new_statement = Select(
                    targets=[Identifier(parts=['table_name'], alias=Identifier('View'))],
                    from_table=Identifier(parts=['information_schema', 'TABLES']),
                    where=where
                )

                query = SQLQuery(
                    new_statement,
                    session=self.session
                )
                return self.answer_select(query)
            elif sql_category == 'plugins':
                if statement.where is not None or statement.like:
                    raise SqlApiException("'SHOW PLUGINS' query should be used without filters")
                new_statement = Select(
                    targets=[Star()],
                    from_table=Identifier(parts=['information_schema', 'PLUGINS'])
                )
                query = SQLQuery(
                    new_statement,
                    session=self.session
                )
                return self.answer_select(query)
            elif sql_category in ('databases', 'schemas'):
                where = statement.where
                if statement.like is not None:
                    like = BinaryOperation('like', args=[Identifier('Database'), Constant(statement.like)])
                    if where is not None:
                        where = BinaryOperation('and', args=[where, like])
                    else:
                        where = like

                new_statement = Select(
                    targets=[Identifier(parts=["NAME"], alias=Identifier('Database'))],
                    from_table=Identifier(parts=['information_schema', 'DATABASES']),
                    where=where
                )
                if statement.where is not None:
                    new_statement.where = statement.where

                if 'FULL' in statement.modes:
                    new_statement.targets.extend([
                        Identifier(parts=['TYPE'], alias=Identifier('TYPE')),
                        Identifier(parts=['ENGINE'], alias=Identifier('ENGINE'))
                    ])

                query = SQLQuery(
                    new_statement,
                    session=self.session
                )
                return self.answer_select(query)
            elif sql_category in ('tables', 'full tables'):
                schema = self.session.database or 'mindsdb'
                if statement.from_table is not None:
                    schema = statement.from_table.parts[-1]
                where = BinaryOperation('and', args=[
                    BinaryOperation('=', args=[Identifier('table_schema'), Constant(schema)]),
                    BinaryOperation('or', args=[
                        BinaryOperation('=', args=[Identifier('table_type'), Constant('BASE TABLE')]),
                        BinaryOperation('or', args=[
                            BinaryOperation('=', args=[Identifier('table_type'), Constant('SYSTEM VIEW')]),
                            BinaryOperation('=', args=[Identifier('table_type'), Constant('VIEW')])
                        ])
                    ])
                ])
                if statement.where is not None:
                    where = BinaryOperation('and', args=[statement.where, where])
                if statement.like is not None:
                    like = BinaryOperation('like', args=[Identifier(f'Tables_in_{schema}'), Constant(statement.like)])
                    if where is not None:
                        where = BinaryOperation('and', args=[where, like])
                    else:
                        where = like

                new_statement = Select(
                    targets=[Identifier(parts=['table_name'], alias=Identifier(f'Tables_in_{schema}'))],
                    from_table=Identifier(parts=['information_schema', 'TABLES']),
                    where=where
                )

                if 'FULL' in statement.modes:
                    new_statement.targets.append(
                        Constant(value='BASE TABLE', alias=Identifier('Table_type'))
                    )

                query = SQLQuery(
                    new_statement,
                    session=self.session
                )
                return self.answer_select(query)
            elif sql_category in ('variables', 'session variables', 'session status', 'global variables'):
                where = statement.where
                if statement.like is not None:
                    like = BinaryOperation('like', args=[Identifier('Variable_name'), Constant(statement.like)])
                    if where is not None:
                        where = BinaryOperation('and', args=[where, like])
                    else:
                        where = like

                new_statement = Select(
                    targets=[Identifier(parts=['Variable_name']), Identifier(parts=['Value'])],
                    from_table=Identifier(parts=['dataframe']),
                    where=where
                )

                data = {}
                is_session = 'session' in sql_category
                for var_name, var_data in SERVER_VARIABLES.items():
                    var_name = var_name.replace('@@', '')
                    if is_session and var_name.startswith('session.') is False:
                        continue
                    if var_name.startswith('session.') or var_name.startswith('GLOBAL.'):
                        name = var_name.replace('session.', '').replace('GLOBAL.', '')
                        data[name] = var_data[0]
                    elif var_name not in data:
                        data[var_name] = var_data[0]

                df = pd.DataFrame(data.items(), columns=['Variable_name', 'Value'])
                data = query_df(df, new_statement)
                data = data.values.tolist()

                columns = [
                    Column(name='Variable_name', table_name='session_variables', type='str'),
                    Column(name='Value', table_name='session_variables', type='str'),
                ]

                return ExecuteAnswer(
                    answer_type=ANSWER_TYPE.TABLE,
                    columns=columns,
                    data=data
                )
            elif "show status like 'ssl_version'" in sql_lower:
                return ExecuteAnswer(
                    answer_type=ANSWER_TYPE.TABLE,
                    columns=[
                        Column(name='Value', table_name='session_variables', type='str'),
                        Column(name='Value', table_name='session_variables', type='str'),
                    ],
                    data=[['Ssl_version', 'TLSv1.1']]
                )
            elif sql_category in ('function status', 'procedure status'):
                # SHOW FUNCTION STATUS WHERE Db = 'MINDSDB';
                # SHOW PROCEDURE STATUS WHERE Db = 'MINDSDB'
                # SHOW FUNCTION STATUS WHERE Db = 'MINDSDB' AND Name LIKE '%';
                return self.answer_function_status()
            elif sql_category in ('index', 'keys', 'indexes'):
                # INDEX | INDEXES | KEYS are synonyms
                # https://dev.mysql.com/doc/refman/8.0/en/show-index.html
                new_statement = Select(
                    targets=[
                        Identifier('TABLE_NAME', alias=Identifier('Table')),
                        Identifier('NON_UNIQUE', alias=Identifier('Non_unique')),
                        Identifier('INDEX_NAME', alias=Identifier('Key_name')),
                        Identifier('SEQ_IN_INDEX', alias=Identifier('Seq_in_index')),
                        Identifier('COLUMN_NAME', alias=Identifier('Column_name')),
                        Identifier('COLLATION', alias=Identifier('Collation')),
                        Identifier('CARDINALITY', alias=Identifier('Cardinality')),
                        Identifier('SUB_PART', alias=Identifier('Sub_part')),
                        Identifier('PACKED', alias=Identifier('Packed')),
                        Identifier('NULLABLE', alias=Identifier('Null')),
                        Identifier('INDEX_TYPE', alias=Identifier('Index_type')),
                        Identifier('COMMENT', alias=Identifier('Comment')),
                        Identifier('INDEX_COMMENT', alias=Identifier('Index_comment')),
                        Identifier('IS_VISIBLE', alias=Identifier('Visible')),
                        Identifier('EXPRESSION', alias=Identifier('Expression'))
                    ],
                    from_table=Identifier(parts=['information_schema', 'STATISTICS']),
                    where=statement.where
                )
                query = SQLQuery(
                    new_statement,
                    session=self.session
                )
                return self.answer_select(query)
            # FIXME if have answer on that request, then DataGrip show warning '[S0022] Column 'Non_unique' not found.'
            elif 'show create table' in sql_lower:
                # SHOW CREATE TABLE `MINDSDB`.`predictors`
                table = sql[sql.rfind('.') + 1:].strip(' .;\n\t').replace('`', '')
                return self.answer_show_create_table(table)
            elif sql_category in ('character set', 'charset'):
                where = statement.where
                if statement.like is not None:
                    like = BinaryOperation('like', args=[Identifier('CHARACTER_SET_NAME'), Constant(statement.like)])
                    if where is not None:
                        where = BinaryOperation('and', args=[where, like])
                    else:
                        where = like
                new_statement = Select(
                    targets=[
                        Identifier('CHARACTER_SET_NAME', alias=Identifier('Charset')),
                        Identifier('DEFAULT_COLLATE_NAME', alias=Identifier('Description')),
                        Identifier('DESCRIPTION', alias=Identifier('Default collation')),
                        Identifier('MAXLEN', alias=Identifier('Maxlen'))
                    ],
                    from_table=Identifier(parts=['INFORMATION_SCHEMA', 'CHARACTER_SETS']),
                    where=where
                )
                query = SQLQuery(
                    new_statement,
                    session=self.session
                )
                return self.answer_select(query)
            elif sql_category == 'warnings':
                return self.answer_show_warnings()
            elif sql_category == 'engines':
                new_statement = Select(
                    targets=[Star()],
                    from_table=Identifier(parts=['information_schema', 'ENGINES'])
                )
                query = SQLQuery(
                    new_statement,
                    session=self.session
                )
                return self.answer_select(query)
            elif sql_category == 'collation':
                where = statement.where
                if statement.like is not None:
                    like = BinaryOperation('like', args=[Identifier('Collation'), Constant(statement.like)])
                    if where is not None:
                        where = BinaryOperation('and', args=[where, like])
                    else:
                        where = like
                new_statement = Select(
                    targets=[
                        Identifier('COLLATION_NAME', alias=Identifier('Collation')),
                        Identifier('CHARACTER_SET_NAME', alias=Identifier('Charset')),
                        Identifier('ID', alias=Identifier('Id')),
                        Identifier('IS_DEFAULT', alias=Identifier('Default')),
                        Identifier('IS_COMPILED', alias=Identifier('Compiled')),
                        Identifier('SORTLEN', alias=Identifier('Sortlen')),
                        Identifier('PAD_ATTRIBUTE', alias=Identifier('Pad_attribute'))
                    ],
                    from_table=Identifier(parts=['INFORMATION_SCHEMA', 'COLLATIONS']),
                    where=where
                )
                query = SQLQuery(
                    new_statement,
                    session=self.session
                )
                return self.answer_select(query)
            elif sql_category == 'table status':
                # TODO improve it
                # SHOW TABLE STATUS LIKE 'table'
                table_name = None
                if statement.like is not None:
                    table_name = statement.like
                # elif condition == 'from' and type(expression) == Identifier:
                #     table_name = expression.parts[-1]
                if table_name is None:
                    err_str = f"Can't determine table name in query: {sql}"
                    log.warning(err_str)
                    raise ErTableExistError(err_str)
                return self.answer_show_table_status(table_name)
            elif sql_category == 'columns':
                is_full = statement.modes is not None and 'full' in statement.modes
                return self.answer_show_columns(statement.from_table, statement.where, statement.like, is_full=is_full)
            else:
                raise ErNotSupportedYet(f'Statement not implemented: {sql}')
        elif type(statement) in (StartTransaction, CommitTransaction, RollbackTransaction):
            return ExecuteAnswer(ANSWER_TYPE.OK)
        elif type(statement) == Set:
            category = (statement.category or '').lower()
            if category == '' and type(statement.arg) == BinaryOperation:
                return ExecuteAnswer(ANSWER_TYPE.OK)
            elif category == 'autocommit':
                return ExecuteAnswer(ANSWER_TYPE.OK)
            elif category == 'names':
                # set names utf8;
                charsets = {
                    'utf8': CHARSET_NUMBERS['utf8_general_ci'],
                    'utf8mb4': CHARSET_NUMBERS['utf8mb4_general_ci']
                }
                self.charset = statement.arg.parts[0]
                self.charset_text_type = charsets.get(self.charset)
                if self.charset_text_type is None:
                    log.warning(f"Unknown charset: {self.charset}. Setting up 'utf8_general_ci' as charset text type.")
                    self.charset_text_type = CHARSET_NUMBERS['utf8_general_ci']
                return ExecuteAnswer(
                    ANSWER_TYPE.OK,
                    state_track=[
                        ['character_set_client', self.charset],
                        ['character_set_connection', self.charset],
                        ['character_set_results', self.charset]
                    ]
                )
            else:
                log.warning(f'SQL statement is not processable, return OK package: {sql}')
                return ExecuteAnswer(ANSWER_TYPE.OK)
        elif type(statement) == Use:
            db_name = statement.value.parts[-1]
            self.change_default_db(db_name)
            return ExecuteAnswer(ANSWER_TYPE.OK)
        elif type(statement) == CreatePredictor:
            return self.answer_create_predictor(statement)
        elif type(statement) == CreateView:
            return self.answer_create_view(statement)
        elif type(statement) == DropView:
            return self.answer_drop_view(statement)
        elif type(statement) == Delete:
            if self.session.database != 'mindsdb' and statement.table.parts[0] != 'mindsdb':
                raise ErBadTableError("Only 'DELETE' from database 'mindsdb' is possible at this moment")
            if statement.table.parts[-1] != 'predictors':
                raise ErBadTableError("Only 'DELETE' from table 'mindsdb.predictors' is possible at this moment")
            self.delete_predictor_query(statement)
            return ExecuteAnswer(ANSWER_TYPE.OK)
        elif type(statement) == Insert:
            if statement.from_select is None:
                raise ErNotSupportedYet("At this moment only 'insert from select' is supported.")
            else:
                SQLQuery(
                    statement,
                    session=self.session,
                    execute=True
                )
                return ExecuteAnswer(ANSWER_TYPE.OK)
        elif type(statement) == Update:
            if statement.from_select is None:
                raise ErNotSupportedYet('Update is not implemented')
            else:
                SQLQuery(
                    statement,
                    session=self.session,
                    execute=True
                )
                return ExecuteAnswer(ANSWER_TYPE.OK)
        elif type(statement) == Alter and ('disable keys' in sql_lower) or ('enable keys' in sql_lower):
            return ExecuteAnswer(ANSWER_TYPE.OK)
        elif type(statement) == Select:
            if statement.from_table is None:
                return self.answer_single_row_select(statement)

            query = SQLQuery(
                statement,
                session=self.session
            )
            return self.answer_select(query)
        elif type(statement) == Union:
            query = SQLQuery(
                statement,
                session=self.session
            )
            return self.answer_select(query)
        elif type(statement) == Explain:
            return self.answer_show_columns(statement.target)
        elif type(statement) == CreateTable:
            # TODO
            return self.answer_apply_predictor(statement)
        else:
            log.warning(f'Unknown SQL statement: {sql}')
            raise ErNotSupportedYet(f'Unknown SQL statement: {sql}')

    def answer_describe_predictor(self, predictor_value):
        predictor_attr = None
        if isinstance(predictor_value, (list, tuple)):
            predictor_name = predictor_value[0]
            predictor_attr = predictor_value[1]
        else:
            predictor_name = predictor_value
        model_controller = self.session.model_controller
        models = model_controller.get_models()
        if predictor_name not in [x['name'] for x in models]:
            raise ErBadTableError(f"Can't describe predictor. There is no predictor with name '{predictor_name}'")
        description = model_controller.get_model_description(predictor_name)

        if predictor_attr is None:
            columns = [
                Column(name='accuracies', table_name='', type='str'),
                Column(name='column_importances', table_name='', type='str'),
                Column(name='outputs', table_name='', type='str'),
                Column(name='inputs', table_name='', type='str'),
                Column(name='model', table_name='', type='str'),
            ]
            description = [
                description['accuracies'],
                description['column_importances'],
                description['outputs'],
                description['inputs'],
                description['model']
            ]
            data = [description]
        else:
            data = model_controller.get_model_data(name=predictor_name)
            if predictor_attr == "features":
                data = self._get_features_info(data)
                columns = [{
                    'table_name': '',
                    'name': 'column',
                    'type': TYPES.MYSQL_TYPE_VAR_STRING
                }, {
                    'table_name': '',
                    'name': 'type',
                    'type': TYPES.MYSQL_TYPE_VAR_STRING
                }, {
                    'table_name': '',
                    'name': "encoder",
                    'type': TYPES.MYSQL_TYPE_VAR_STRING
                }, {
                    'table_name': '',
                    'name': 'role',
                    'type': TYPES.MYSQL_TYPE_VAR_STRING
                }]
                columns = [Column(**d) for d in columns]
            elif predictor_attr == "model":
                data = self._get_model_info(data)
                columns = [{
                    'table_name': '',
                    'name': 'name',
                    'type': TYPES.MYSQL_TYPE_VAR_STRING
                }, {
                    'table_name': '',
                    'name': 'performance',
                    'type': TYPES.MYSQL_TYPE_VAR_STRING
                }, {
                    'table_name': '',
                    'name': 'training_time',
                    'type': TYPES.MYSQL_TYPE_VAR_STRING
                }, {
                    'table_name': '',
                    'name': "selected",
                    'type': TYPES.MYSQL_TYPE_VAR_STRING
                }, {
                    'table_name': '',
                    'name': "accuracy_functions",
                    'type': TYPES.MYSQL_TYPE_VAR_STRING
                }]
                columns = [Column(**d) for d in columns]
            elif predictor_attr == "ensemble":
                data = self._get_ensemble_data(data)
                columns = [
                    Column(name='ensemble', table_name='', type='str')
                ]
            else:
                raise ErNotSupportedYet("DESCRIBE '%s' predictor attribute is not supported yet" % predictor_attr)

        return ExecuteAnswer(
            answer_type=ANSWER_TYPE.TABLE,
            columns=columns,
            data=data
        )

    def answer_retrain_predictor(self, statement):
        if len(statement.name.parts) == 1:
            statement.name.parts = [
                self.session.database,
                statement.name.parts[0]
            ]
        database_name, model_name = statement.name.parts

        model_record = get_model_record(
            company_id=self.session.company_id,
            name=model_name,
            project_name=database_name,
            except_absent=True
        )
        integration_record = get_predictor_integration(model_record)
        if integration_record is None:
            raise Exception(f"Model '{model_name}' does not have linked integration")

        ml_handler = self.session.integration_controller.get_handler(integration_record.name)

        # region check if there is already predictor retraing
        is_cloud = self.session.config.get('cloud', False)
        if is_cloud and self.session.user_class == 0:
            models = get_model_records(
                company_id=self.session.company_id,
                active=None
            )
            longest_training = None
            for p in models:
                if (
                    p.status in (PREDICTOR_STATUS.GENERATING, PREDICTOR_STATUS.TRAINING)
                    and p.training_start_at is not None and p.training_stop_at is None
                ):
                    training_time = datetime.datetime.now() - p.training_start_at
                    if longest_training is None or training_time > longest_training:
                        longest_training = training_time
            if longest_training is not None and longest_training > datetime.timedelta(hours=1):
                raise SqlApiException(
                    "Can't start retrain while exists predictor in status 'training' or 'generating'"
                )
        # endregion

        result = ml_handler.query(statement)
        if result.type == RESPONSE_TYPE.ERROR:
            raise Exception(result.error_message)

        return ExecuteAnswer(ANSWER_TYPE.OK)

    def _create_integration(self, name: str, engine: str, connection_args: dict):
        # we have connection checkers not for any db. So do nothing if fail
        # TODO return rich error message

        status = HandlerStatusResponse(success=False)

        try:
            handlers_meta = self.session.integration_controller.get_handlers_import_status()
            handler_meta = handlers_meta[engine]
            if handler_meta.get('import', {}).get('success') is not True:
                raise SqlApiException(f"Handler '{engine}' can not be used")

            accept_connection_args = handler_meta.get('connection_args')
            if accept_connection_args is not None:
                for arg_name, arg_value in connection_args.items():
                    if arg_name == 'as_service':
                        continue
                    if arg_name not in accept_connection_args:
                        raise SqlApiException(f"Unknown connection argument: {arg_name}")
                    arg_meta = accept_connection_args[arg_name]
                    arg_type = arg_meta.get('type')
                    if arg_type == HANDLER_CONNECTION_ARG_TYPE.PATH:
                        # arg may be one of:
                        # str: '/home/file.pem'
                        # dict: {'path': '/home/file.pem'}
                        # dict: {'url': 'https://host.com/file'}
                        arg_value = connection_args[arg_name]
                        if isinstance(arg_value, (str, dict)) is False:
                            raise SqlApiException(f"Unknown type of arg: '{arg_value}'")
                        if isinstance(arg_value, str) or 'path' in arg_value:
                            path = arg_value if isinstance(arg_value, str) else arg_value['path']
                            if Path(path).is_file() is False:
                                raise SqlApiException(f"File not found at: '{path}'")
                        elif 'url' in arg_value:
                            path = download_file(arg_value['url'])
                        else:
                            raise SqlApiException(f"Argument '{arg_name}' must be path or url to the file")
                        connection_args[arg_name] = path

            handler = self.session.integration_controller.create_tmp_handler(
                handler_type=engine,
                connection_data=connection_args
            )
            status = handler.check_connection()
        except Exception as e:
            status.error_message = str(e)

        if status.success is False:
            raise SqlApiException(f"Can't connect to db: {status.error_message}")

        integration = self.session.integration_controller.get(name)
        if integration is not None:
            raise SqlApiException(f"Database '{name}' already exists.")

        self.session.integration_controller.add(name, engine, connection_args)

    def answer_create_database(self, statement: ASTNode):
        ''' create new handler (datasource/integration in old terms)
            Args:
                statement (ASTNode): data for creating database/project
        '''

        database_name = statement.name
        engine = statement.engine
        if engine is None:
            engine = 'mindsdb'
        engine = engine.lower()
        connection_args = statement.parameters

        if engine == 'mindsdb':
            self.session.project_controller.add(database_name)
        else:
            self._create_integration(database_name, engine, connection_args)

        return ExecuteAnswer(ANSWER_TYPE.OK)

    def answer_drop_database(self, statement):
        if len(statement.name.parts) != 1:
            raise Exception('Database name should contain only 1 part.')
        db_name = statement.name.parts[0]
        self.session.database_controller.delete(db_name)
        return ExecuteAnswer(ANSWER_TYPE.OK)

    def answer_drop_tables(self, statement):
        """ answer on 'drop table [if exists] {name}'
            Args:
                statement: ast
        """
        if statement.if_exists is False:
            for table in statement.tables:
                if len(table.parts) > 1:
                    db_name = table.parts[0]
                else:
                    db_name = self.session.database
                table_name = table.parts[-1]

                if db_name == 'files':
                    dn = self.session.datahub[db_name]
                    if dn.has_table(table_name) is False:
                        raise SqlApiException(f"Cannot delete a table from database '{db_name}': table does not exists")
                else:
                    projects_dict = self.session.database_controller.get_dict(filter_type='project')
                    if db_name not in projects_dict:
                        raise SqlApiException(f"Cannot delete a table from database '{db_name}'")
                    project = self.session.database_controller.get_project(db_name)
                    project_tables = {key: val for key, val in project.get_tables().items() if val.get('deletable') is True}
                    if table_name not in project_tables:
                        raise SqlApiException(f"Cannot delete a table from database '{db_name}': table does not exists")

        for table in statement.tables:
            if len(table.parts) > 1:
                db_name = table.parts[0]
            else:
                db_name = self.session.database
            table_name = table.parts[-1]

            if db_name == 'files':
                dn = self.session.datahub[db_name]
                if dn.has_table(table_name):
                    self.session.datahub['files'].query(
                        DropTables(tables=[Identifier(table_name)])
                    )
            else:
                projects_dict = self.session.database_controller.get_dict(filter_type='project')
                if db_name not in projects_dict:
                    continue
                self.session.model_controller.delete_model(table_name, project_name=db_name)
        return ExecuteAnswer(ANSWER_TYPE.OK)

    def answer_create_view(self, statement):
        name = statement.name

        query_str = statement.query_str
        query = parse_sql(query_str, dialect='mindsdb')

        integration_name = None
        if statement.from_table is not None:
            integration_name = statement.from_table.parts[-1]

        if integration_name is not None:

            # inject integration into sql
            query = parse_sql(query_str, dialect='mindsdb')

            def inject_integration(node, is_table, **kwargs):
                if is_table and isinstance(node, Identifier):
                    if not node.parts[0] == integration_name:
                        node.parts.insert(0, integration_name)

            query_traversal(query, inject_integration)

            render = SqlalchemyRender('mysql')
            query_str = render.get_string(query, with_failback=False)

        if isinstance(query, Select):
            # check create view sql
            query.limit = Constant(1)

            # exception should appear from SQLQuery
            sqlquery = SQLQuery(query, session=self.session)
            if sqlquery.fetch()['success'] != True:
                raise SqlApiException('Wrong view query')

        self.session.view_interface.add(name, query_str, integration_name)
        return ExecuteAnswer(answer_type=ANSWER_TYPE.OK)

    def answer_drop_view(self, statement):
        names = statement.names

        for name in names:
            view_name = name.parts[-1]
            self.session.view_interface.delete(view_name)

        return ExecuteAnswer(answer_type=ANSWER_TYPE.OK)

    def answer_create_predictor(self, statement):
        integration_name = self.session.database
        if len(statement.name.parts) > 1:
            integration_name = statement.name.parts[0]
        else:
            statement.name.parts = [integration_name, statement.name.parts[-1]]
        integration_name = integration_name.lower()

        ml_integration_name = 'lightwood'
        if statement.using is not None and statement.using.get('engine') is not None:
            using = {k.lower(): v for k, v in statement.using.items()}
            ml_integration_name = using.get('engine', ml_integration_name)

        ml_handler = self.session.integration_controller.get_handler(ml_integration_name)

        result = ml_handler.query(statement)
        if result.type == RESPONSE_TYPE.ERROR:
            raise Exception(result.error_message)

        return ExecuteAnswer(ANSWER_TYPE.OK)

    def delete_predictor_query(self, query):

        query2 = Select(targets=[Identifier('name')],
                        from_table=query.table,
                        where=query.where)

        sqlquery = SQLQuery(
            query2.to_string(),
            session=self.session
        )

        result = sqlquery.fetch(
            self.session.datahub
        )

        predictors_names = [x[0] for x in result['result']]

        if len(predictors_names) == 0:
            raise SqlApiException('nothing to delete')

        for predictor_name in predictors_names:
            self.session.datahub['mindsdb'].delete_predictor(predictor_name)

    def answer_show_columns(self, target: Identifier, where: Optional[Operation] = None,
                            like: Optional[str] = None, is_full=False):
        if len(target.parts) > 1:
            db = target.parts[0]
        elif isinstance(self.session.database, str) and len(self.session.database) > 0:
            db = self.session.database
        else:
            db = 'mindsdb'
        table_name = target.parts[-1]

        new_where = BinaryOperation('and', args=[
            BinaryOperation('=', args=[Identifier('TABLE_SCHEMA'), Constant(db)]),
            BinaryOperation('=', args=[Identifier('TABLE_NAME'), Constant(table_name)])
        ])
        if where is not None:
            new_where = BinaryOperation('and', args=[new_where, where])
        if like is not None:
            like = BinaryOperation('like', args=[Identifier('View'), Constant(like)])
            new_where = BinaryOperation('and', args=[new_where, like])

        targets = [
            Identifier('COLUMN_NAME', alias=Identifier('Field')),
            Identifier('COLUMN_TYPE', alias=Identifier('Type')),
            Identifier('IS_NULLABLE', alias=Identifier('Null')),
            Identifier('COLUMN_KEY', alias=Identifier('Key')),
            Identifier('COLUMN_DEFAULT', alias=Identifier('Default')),
            Identifier('EXTRA', alias=Identifier('Extra'))
        ]
        if is_full:
            targets.extend([
                Constant('COLLATION', alias=Identifier('Collation')),
                Constant('PRIVILEGES', alias=Identifier('Privileges')),
                Constant('COMMENT', alias=Identifier('Comment')),
            ])
        new_statement = Select(
            targets=targets,
            from_table=Identifier(parts=['information_schema', 'COLUMNS']),
            where=new_where
        )

        query = SQLQuery(
            new_statement,
            session=self.session
        )
        return self.answer_select(query)

    def answer_single_row_select(self, statement):
        columns = []
        data = []
        for target in statement.targets:
            target_type = type(target)
            if target_type == Variable:
                var_name = target.value
                column_name = f'@@{var_name}'
                column_alias = target.alias or column_name
                result = SERVER_VARIABLES.get(column_name)
                if result is None:
                    log.error(f'Unknown variable: {column_name}')
                    raise Exception(f"Unknown variable '{var_name}'")
                else:
                    result = result[0]
            elif target_type == Function:
                function_name = target.op.lower()
                if function_name == 'connection_id':
                    return self.answer_connection_id()

                functions_results = {
                    # 'connection_id': self.executor.sqlserver.connection_id,
                    'database': self.session.database,
                    'current_user': self.session.username,
                    'user': self.session.username,
                    'version': '8.0.17'
                }

                column_name = f'{target.op}()'
                column_alias = target.alias or column_name
                result = functions_results[function_name]
            elif target_type == Constant:
                result = target.value
                column_name = str(result)
                column_alias = '.'.join(target.alias.parts) if type(target.alias) == Identifier else column_name
            elif target_type == NullConstant:
                result = None
                column_name = 'NULL'
                column_alias = 'NULL'
            elif target_type == Identifier:
                result = '.'.join(target.parts)
                raise Exception(f"Unknown column '{result}'")
            else:
                raise ErSqlWrongArguments(f'Unknown constant type: {target_type}')

            columns.append(
                Column(
                    name=column_name, alias=column_alias,
                    table_name='',
                    type=TYPES.MYSQL_TYPE_VAR_STRING if isinstance(result, str) else TYPES.MYSQL_TYPE_LONG,
                    charset=self.charset_text_type if isinstance(result, str) else CHARSET_NUMBERS['binary']
                )
            )
            data.append(result)

        return ExecuteAnswer(
            answer_type=ANSWER_TYPE.TABLE,
            columns=columns,
            data=[data]
        )

    def answer_show_create_table(self, table):
        columns = [
            Column(table_name='', name='Table', type=TYPES.MYSQL_TYPE_VAR_STRING),
            Column(table_name='', name='Create Table', type=TYPES.MYSQL_TYPE_VAR_STRING),
        ]
        return ExecuteAnswer(
            answer_type=ANSWER_TYPE.TABLE,
            columns=columns,
            data=[[table, f'create table {table} ()']]
        )

    def answer_function_status(self):
        columns = [
            Column(name='Db', alias='Db',
                   table_name='schemata', table_alias='ROUTINES',
                   type='str', database='mysql', charset=self.charset_text_type),
            Column(name='Db', alias='Db',
                   table_name='routines', table_alias='ROUTINES',
                   type='str', database='mysql', charset=self.charset_text_type),
            Column(name='Type', alias='Type',
                   table_name='routines', table_alias='ROUTINES',
                   type='str', database='mysql', charset=CHARSET_NUMBERS['utf8_bin']),
            Column(name='Definer', alias='Definer',
                   table_name='routines', table_alias='ROUTINES',
                   type='str', database='mysql', charset=CHARSET_NUMBERS['utf8_bin']),
            Column(name='Modified', alias='Modified',
                   table_name='routines', table_alias='ROUTINES',
                   type=TYPES.MYSQL_TYPE_TIMESTAMP, database='mysql',
                   charset=CHARSET_NUMBERS['binary']),
            Column(name='Created', alias='Created',
                   table_name='routines', table_alias='ROUTINES',
                   type=TYPES.MYSQL_TYPE_TIMESTAMP, database='mysql',
                   charset=CHARSET_NUMBERS['binary']),
            Column(name='Security_type', alias='Security_type',
                   table_name='routines', table_alias='ROUTINES',
                   type=TYPES.MYSQL_TYPE_STRING, database='mysql',
                   charset=CHARSET_NUMBERS['utf8_bin']),
            Column(name='Comment', alias='Comment',
                   table_name='routines', table_alias='ROUTINES',
                   type=TYPES.MYSQL_TYPE_BLOB, database='mysql',
                   charset=CHARSET_NUMBERS['utf8_bin']),
            Column(name='character_set_client', alias='character_set_client',
                   table_name='character_sets', table_alias='ROUTINES',
                   type=TYPES.MYSQL_TYPE_VAR_STRING, database='mysql',
                   charset=self.charset_text_type),
            Column(name='collation_connection', alias='collation_connection',
                   table_name='collations', table_alias='ROUTINES',
                   type=TYPES.MYSQL_TYPE_VAR_STRING, database='mysql',
                   charset=self.charset_text_type),
            Column(name='Database Collation', alias='Database Collation',
                   table_name='collations', table_alias='ROUTINES',
                   type=TYPES.MYSQL_TYPE_VAR_STRING, database='mysql',
                   charset=self.charset_text_type)
        ]

        return ExecuteAnswer(
            answer_type=ANSWER_TYPE.TABLE,
            columns=columns,
            data=[]
        )

    def answer_show_table_status(self, table_name):
        # NOTE at this moment parsed statement only like `SHOW TABLE STATUS LIKE 'table'`.
        # NOTE some columns has {'database': 'mysql'}, other not. That correct. This is how real DB sends messages.
        columns = [{
            'database': 'mysql',
            'table_name': 'tables',
            'name': 'Name',
            'alias': 'Name',
            'type': TYPES.MYSQL_TYPE_VAR_STRING,
            'charset': self.charset_text_type
        }, {
            'database': '',
            'table_name': 'tables',
            'name': 'Engine',
            'alias': 'Engine',
            'type': TYPES.MYSQL_TYPE_VAR_STRING,
            'charset': self.charset_text_type
        }, {
            'database': '',
            'table_name': 'tables',
            'name': 'Version',
            'alias': 'Version',
            'type': TYPES.MYSQL_TYPE_LONGLONG,
            'charset': CHARSET_NUMBERS['binary']
        }, {
            'database': 'mysql',
            'table_name': 'tables',
            'name': 'Row_format',
            'alias': 'Row_format',
            'type': TYPES.MYSQL_TYPE_VAR_STRING,
            'charset': self.charset_text_type
        }, {
            'database': '',
            'table_name': 'tables',
            'name': 'Rows',
            'alias': 'Rows',
            'type': TYPES.MYSQL_TYPE_LONGLONG,
            'charset': CHARSET_NUMBERS['binary']
        }, {
            'database': '',
            'table_name': 'tables',
            'name': 'Avg_row_length',
            'alias': 'Avg_row_length',
            'type': TYPES.MYSQL_TYPE_LONGLONG,
            'charset': CHARSET_NUMBERS['binary']
        }, {
            'database': '',
            'table_name': 'tables',
            'name': 'Data_length',
            'alias': 'Data_length',
            'type': TYPES.MYSQL_TYPE_LONGLONG,
            'charset': CHARSET_NUMBERS['binary']
        }, {
            'database': '',
            'table_name': 'tables',
            'name': 'Max_data_length',
            'alias': 'Max_data_length',
            'type': TYPES.MYSQL_TYPE_LONGLONG,
            'charset': CHARSET_NUMBERS['binary']
        }, {
            'database': '',
            'table_name': 'tables',
            'name': 'Index_length',
            'alias': 'Index_length',
            'type': TYPES.MYSQL_TYPE_LONGLONG,
            'charset': CHARSET_NUMBERS['binary']
        }, {
            'database': '',
            'table_name': 'tables',
            'name': 'Data_free',
            'alias': 'Data_free',
            'type': TYPES.MYSQL_TYPE_LONGLONG,
            'charset': CHARSET_NUMBERS['binary']
        }, {
            'database': '',
            'table_name': 'tables',
            'name': 'Auto_increment',
            'alias': 'Auto_increment',
            'type': TYPES.MYSQL_TYPE_LONGLONG,
            'charset': CHARSET_NUMBERS['binary']
        }, {
            'database': '',
            'table_name': 'tables',
            'name': 'Create_time',
            'alias': 'Create_time',
            'type': TYPES.MYSQL_TYPE_TIMESTAMP,
            'charset': CHARSET_NUMBERS['binary']
        }, {
            'database': '',
            'table_name': 'tables',
            'name': 'Update_time',
            'alias': 'Update_time',
            'type': TYPES.MYSQL_TYPE_TIMESTAMP,
            'charset': CHARSET_NUMBERS['binary']
        }, {
            'database': '',
            'table_name': 'tables',
            'name': 'Check_time',
            'alias': 'Check_time',
            'type': TYPES.MYSQL_TYPE_TIMESTAMP,
            'charset': CHARSET_NUMBERS['binary']
        }, {
            'database': 'mysql',
            'table_name': 'tables',
            'name': 'Collation',
            'alias': 'Collation',
            'type': TYPES.MYSQL_TYPE_VAR_STRING,
            'charset': self.charset_text_type
        }, {
            'database': '',
            'table_name': 'tables',
            'name': 'Checksum',
            'alias': 'Checksum',
            'type': TYPES.MYSQL_TYPE_LONGLONG,
            'charset': CHARSET_NUMBERS['binary']
        }, {
            'database': '',
            'table_name': 'tables',
            'name': 'Create_options',
            'alias': 'Create_options',
            'type': TYPES.MYSQL_TYPE_VAR_STRING,
            'charset': self.charset_text_type
        }, {
            'database': '',
            'table_name': 'tables',
            'name': 'Comment',
            'alias': 'Comment',
            'type': TYPES.MYSQL_TYPE_BLOB,
            'charset': self.charset_text_type
        }]
        columns = [Column(**d) for d in columns]
        data = [[
            table_name,     # Name
            'InnoDB',       # Engine
            10,             # Version
            'Dynamic',      # Row_format
            1,              # Rows
            16384,          # Avg_row_length
            16384,          # Data_length
            0,              # Max_data_length
            0,              # Index_length
            0,              # Data_free
            None,           # Auto_increment
            datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),  # Create_time
            datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),  # Update_time
            None,           # Check_time
            'utf8mb4_0900_ai_ci',   # Collation
            None,           # Checksum
            '',             # Create_options
            ''              # Comment
        ]]
        return ExecuteAnswer(
            answer_type=ANSWER_TYPE.TABLE,
            columns=columns,
            data=data
        )

    def answer_show_warnings(self):
        columns = [{
            'database': '',
            'table_name': '',
            'name': 'Level',
            'alias': 'Level',
            'type': TYPES.MYSQL_TYPE_VAR_STRING,
            'charset': self.charset_text_type
        }, {
            'database': '',
            'table_name': '',
            'name': 'Code',
            'alias': 'Code',
            'type': TYPES.MYSQL_TYPE_LONG,
            'charset': CHARSET_NUMBERS['binary']
        }, {
            'database': '',
            'table_name': '',
            'name': 'Message',
            'alias': 'Message',
            'type': TYPES.MYSQL_TYPE_VAR_STRING,
            'charset': self.charset_text_type
        }]
        columns = [Column(**d) for d in columns]
        return ExecuteAnswer(
            answer_type=ANSWER_TYPE.TABLE,
            columns=columns,
            data=[]
        )

    def answer_connection_id(self):
        columns = [{
            'database': '',
            'table_name': '',
            'name': 'conn_id',
            'alias': 'conn_id',
            'type': TYPES.MYSQL_TYPE_LONG,
            'charset': CHARSET_NUMBERS['binary']
        }]
        columns = [Column(**d) for d in columns]
        data = [[self.executor.sqlserver.connection_id]]
        return ExecuteAnswer(
            answer_type=ANSWER_TYPE.TABLE,
            columns=columns,
            data=data
        )

    def answer_apply_predictor(self, statement):
        SQLQuery(
            statement,
            session=self.session,
            execute=True
        )
        return ExecuteAnswer(ANSWER_TYPE.OK)

    def answer_select(self, query):
        data = query.fetch()

        return ExecuteAnswer(
            answer_type=ANSWER_TYPE.TABLE,
            columns=query.columns_list,
            data=data['result'],
        )

    def change_default_db(self, db_name):
        # That fix for bug in mssql: it keeps connection for a long time, but after some time mssql can
        # send packet with COM_INIT_DB=null. In this case keep old database name as default.
        if db_name != 'null':
            if self.session.database_controller.exists(db_name):
                self.session.database = db_name
            else:
                raise ErBadDbError(f"Database {db_name} does not exists")

    def _get_features_info(self, data):
        ai_info = data.get('json_ai', {})
        if ai_info == {}:
            raise ErBadTableError("predictor doesn't contain enough data to generate 'feature' attribute.")
        data = []
        dtype_dict = ai_info["dtype_dict"]
        for column in dtype_dict:
            c_data = []
            c_data.append(column)
            c_data.append(dtype_dict[column])
            c_data.append(ai_info["encoders"][column]["module"])
            if ai_info["encoders"][column]["args"].get("is_target", "False") == "True":
                c_data.append("target")
            else:
                c_data.append("feature")
            data.append(c_data)
        return data

    def _get_model_info(self, data):
        accuracy_functions = data.get('json_ai', {}).get('accuracy_functions')
        if accuracy_functions:
            accuracy_functions = str(accuracy_functions)

        models_data = data.get("submodel_data", [])
        if models_data == []:
            raise ErBadTableError("predictor doesn't contain enough data to generate 'model' attribute")
        data = []

        for model in models_data:
            m_data = []
            m_data.append(model["name"])
            m_data.append(model["accuracy"])
            m_data.append(model.get("training_time", "unknown"))
            m_data.append(1 if model["is_best"] else 0)
            m_data.append(accuracy_functions)
            data.append(m_data)
        return data

    def _get_ensemble_data(self, data):
        ai_info = data.get('json_ai', {})
        if ai_info == {}:
            raise ErBadTableError("predictor doesn't contain enough data to generate 'ensamble' attribute. Please wait until predictor is complete.")
        ai_info_str = json.dumps(ai_info, indent=2)
        return [[ai_info_str]]
