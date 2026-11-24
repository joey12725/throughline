package throughline

import (
	"context"
	"errors"
	"fmt"
	"io"
	"regexp"
	"strconv"
	"strings"
)

var (
	relationRe = regexp.MustCompile(`^[a-z][a-z0-9_]*$`)
	channelRe  = regexp.MustCompile(`^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$`)
	paramRefRe = regexp.MustCompile(`\$(\d+)`)
)

type joinSpec struct {
	right      *DataFrame
	leftCol    string
	rightCol   string
	onExpr     *Expr
	how        string
	rightAlias string
}

// DataFrame is an immutable, lazy pushdown query builder over Throughline api.* views.
type DataFrame struct {
	session      *Session
	sourceSQL    string
	sourceParams []any
	sourceAlias  string
	selects      []Expr
	filters      []Expr
	groupBy      []Expr
	having       []Expr
	joins        []joinSpec
	orders       []OrderExpr
	distinct     bool
	limit        *int
	offset       *int
	err          error
}

// GroupedDataFrame is returned by DataFrame.GroupBy.
type GroupedDataFrame struct {
	df        *DataFrame
	groupCols []Expr
}

// Table opens a lazy DataFrame over an api.<name> view (e.g. "messages", "threads", "people").
func (s *Session) Table(name string) *DataFrame {
	clean := strings.TrimPrefix(name, "api.")
	if !relationRe.MatchString(clean) {
		return &DataFrame{session: s, err: fmt.Errorf("invalid api view name: %q", name)}
	}
	return &DataFrame{session: s, sourceSQL: "api." + clean}
}

// Channel opens a lazy DataFrame over a channel's typed view (api.ch_<channel>).
func (s *Session) Channel(name string) *DataFrame {
	if !channelRe.MatchString(name) {
		return &DataFrame{session: s, err: fmt.Errorf("invalid channel name: %q", name)}
	}
	return &DataFrame{session: s, sourceSQL: "api.ch_" + strings.ReplaceAll(name, ".", "_")}
}

// Lens opens a lazy DataFrame over a lens view (api.lens_<name>).
func (s *Session) Lens(name string) *DataFrame {
	if !relationRe.MatchString(name) {
		return &DataFrame{session: s, err: fmt.Errorf("invalid lens name: %q", name)}
	}
	return &DataFrame{session: s, sourceSQL: "api.lens_" + name}
}

// FromSQL wraps a SELECT statement so further Snowpark transforms can be chained lazily.
func (s *Session) FromSQL(statement string, params ...any) *DataFrame {
	trimmed := strings.TrimRight(strings.TrimSpace(statement), ";")
	return &DataFrame{
		session:      s,
		sourceSQL:    "(" + trimmed + ")",
		sourceParams: append([]any(nil), params...),
		sourceAlias:  "_q",
	}
}

func (df *DataFrame) clone() *DataFrame {
	cp := *df
	cp.sourceParams = append([]any(nil), df.sourceParams...)
	cp.selects = append([]Expr(nil), df.selects...)
	cp.filters = append([]Expr(nil), df.filters...)
	cp.groupBy = append([]Expr(nil), df.groupBy...)
	cp.having = append([]Expr(nil), df.having...)
	cp.joins = append([]joinSpec(nil), df.joins...)
	cp.orders = append([]OrderExpr(nil), df.orders...)
	return &cp
}

func (df *DataFrame) needsSubquery() bool {
	return len(df.selects) > 0 || len(df.groupBy) > 0 || df.distinct || df.limit != nil || df.offset != nil
}

func (df *DataFrame) needsSubqueryForFilter() bool {
	for _, s := range df.selects {
		if s.alias != "" {
			return true
		}
	}
	return len(df.groupBy) > 0 || df.distinct || df.limit != nil || df.offset != nil
}

func (df *DataFrame) wrap(alias string) *DataFrame {
	sql, params, err := df.ToSQL()
	if err != nil {
		return &DataFrame{session: df.session, err: err}
	}
	return &DataFrame{
		session:      df.session,
		sourceSQL:    "(" + sql + ")",
		sourceParams: params,
		sourceAlias:  alias,
	}
}

// Alias sets a SQL alias on this relation.
func (df *DataFrame) Alias(name string) *DataFrame {
	if df.err != nil {
		return df.clone()
	}
	if !identRe.MatchString(name) {
		return &DataFrame{session: df.session, err: fmt.Errorf("invalid alias: %q", name)}
	}
	if df.needsSubquery() || len(df.filters) > 0 || len(df.joins) > 0 {
		return df.wrap(name)
	}
	cp := df.clone()
	cp.sourceAlias = name
	return cp
}

// Select projects named columns.
func (df *DataFrame) Select(columns ...string) *DataFrame {
	exprs := make([]Expr, len(columns))
	for i, c := range columns {
		exprs[i] = Col(c)
	}
	return df.SelectExpr(exprs...)
}

// SelectExpr projects arbitrary SQL expressions.
func (df *DataFrame) SelectExpr(exprs ...Expr) *DataFrame {
	if df.err != nil {
		return df.clone()
	}
	if len(exprs) == 0 {
		return &DataFrame{session: df.session, err: errors.New("Select requires at least one column")}
	}
	base := df
	if df.needsSubquery() {
		base = df.wrap("_q")
	} else {
		base = df.clone()
	}
	base.selects = append([]Expr(nil), exprs...)
	return base
}

// WithColumn adds a computed column alongside existing columns.
func (df *DataFrame) WithColumn(name string, expr Expr) *DataFrame {
	if df.err != nil {
		return df.clone()
	}
	base := df
	if df.needsSubquery() {
		base = df.wrap("_q")
	} else {
		base = df.clone()
	}
	if len(base.selects) == 0 {
		base.selects = []Expr{Col("*")}
	}
	base.selects = append(base.selects, expr.As(name))
	return base
}

// Where adds a filter predicate (alias: Filter).
func (df *DataFrame) Where(condition Expr) *DataFrame {
	if df.err != nil {
		return df.clone()
	}
	base := df
	if df.needsSubqueryForFilter() {
		base = df.wrap("_q")
	} else {
		base = df.clone()
	}
	base.filters = append(base.filters, condition)
	return base
}

// Filter adds a filter predicate.
func (df *DataFrame) Filter(condition Expr) *DataFrame { return df.Where(condition) }

// GroupBy starts a server-side aggregation over column names.
func (df *DataFrame) GroupBy(columns ...string) *GroupedDataFrame {
	exprs := make([]Expr, len(columns))
	for i, c := range columns {
		exprs[i] = Col(c)
	}
	return df.GroupByExpr(exprs...)
}

// GroupByExpr starts a server-side aggregation over arbitrary expressions.
func (df *DataFrame) GroupByExpr(exprs ...Expr) *GroupedDataFrame {
	base := df
	if df.err == nil && df.needsSubquery() {
		base = df.wrap("_q")
	} else {
		base = df.clone()
	}
	return &GroupedDataFrame{df: base, groupCols: append([]Expr(nil), exprs...)}
}

// Agg finishes a GroupBy with aggregate expressions.
func (g *GroupedDataFrame) Agg(aggs ...Expr) *DataFrame {
	if g.df.err != nil {
		return g.df.clone()
	}
	if len(aggs) == 0 {
		return &DataFrame{session: g.df.session, err: errors.New("Agg requires at least one expression")}
	}
	cp := g.df.clone()
	cp.groupBy = append([]Expr(nil), g.groupCols...)
	cp.selects = append(append([]Expr(nil), g.groupCols...), aggs...)
	return cp
}

// Count finishes a GroupBy with count(*) AS count.
func (g *GroupedDataFrame) Count() *DataFrame {
	return g.Agg(Count("*").As("count"))
}

// Having adds a HAVING predicate to a grouped query.
func (g *GroupedDataFrame) Having(cond Expr) *GroupedDataFrame {
	cp := g.df.clone()
	cp.having = append(cp.having, cond)
	return &GroupedDataFrame{df: cp, groupCols: g.groupCols}
}

// Join joins another DataFrame on matching column names (leftCol = rightCol).
func (df *DataFrame) Join(other *DataFrame, leftCol, rightCol, how string) *DataFrame {
	if df.err != nil {
		return df.clone()
	}
	if other.err != nil {
		return &DataFrame{session: df.session, err: other.err}
	}
	joinType := strings.ToLower(how)
	if joinType == "" {
		joinType = "inner"
	}
	switch joinType {
	case "inner", "left", "right", "full":
	default:
		return &DataFrame{session: df.session, err: fmt.Errorf("unsupported join type: %q", how)}
	}
	left := df
	if left.needsSubquery() {
		left = left.wrap("l")
	} else {
		left = left.clone()
		if left.sourceAlias == "" {
			left.sourceAlias = "l"
		}
	}
	rAlias := other.sourceAlias
	if rAlias == "" {
		rAlias = "r"
	}
	if rAlias == left.sourceAlias {
		rAlias = fmt.Sprintf("%s_%d", rAlias, len(left.joins)+1)
	}
	left.joins = append(left.joins, joinSpec{
		right:      other,
		leftCol:    leftCol,
		rightCol:   rightCol,
		how:        joinType,
		rightAlias: rAlias,
	})
	return left
}

// OrderBy sets ORDER BY expressions.
func (df *DataFrame) OrderBy(orders ...OrderExpr) *DataFrame {
	if df.err != nil {
		return df.clone()
	}
	cp := df.clone()
	cp.orders = append([]OrderExpr(nil), orders...)
	return cp
}

// Distinct adds SELECT DISTINCT.
func (df *DataFrame) Distinct() *DataFrame {
	if df.err != nil {
		return df.clone()
	}
	cp := df.clone()
	cp.distinct = true
	return cp
}

// Limit caps the number of rows returned.
func (df *DataFrame) Limit(n int) *DataFrame {
	if df.err != nil {
		return df.clone()
	}
	if n < 0 {
		return &DataFrame{session: df.session, err: errors.New("Limit must be non-negative")}
	}
	cp := df.clone()
	cp.limit = &n
	return cp
}

// Offset skips n rows.
func (df *DataFrame) Offset(n int) *DataFrame {
	if df.err != nil {
		return df.clone()
	}
	if n < 0 {
		return &DataFrame{session: df.session, err: errors.New("Offset must be non-negative")}
	}
	cp := df.clone()
	cp.offset = &n
	return cp
}

func rebindParams(sqlText string, params []any, p *paramCollector) string {
	if len(params) == 0 {
		return sqlText
	}
	base := len(p.values)
	p.values = append(p.values, params...)
	if base == 0 {
		return sqlText
	}
	return paramRefRe.ReplaceAllStringFunc(sqlText, func(match string) string {
		n, _ := strconv.Atoi(match[1:])
		return fmt.Sprintf("$%d", n+base)
	})
}

func (df *DataFrame) compileInto(p *paramCollector) (string, error) {
	if df.err != nil {
		return "", df.err
	}
	sourceSQL := rebindParams(df.sourceSQL, df.sourceParams, p)

	proj := "*"
	if len(df.selects) > 0 {
		cols := make([]string, len(df.selects))
		for i, c := range df.selects {
			s, err := c.compileSelect(p)
			if err != nil {
				return "", err
			}
			cols[i] = s
		}
		proj = strings.Join(cols, ", ")
	}
	distinctKW := ""
	if df.distinct {
		distinctKW = "DISTINCT "
	}
	fromClause := sourceSQL
	if df.sourceAlias != "" {
		qa, err := quoteIdent(df.sourceAlias)
		if err != nil {
			return "", err
		}
		fromClause += " AS " + qa
	}

	var parts []string
	parts = append(parts, "SELECT "+distinctKW+proj+" FROM "+fromClause)

	joinKW := map[string]string{
		"inner": "INNER JOIN",
		"left":  "LEFT JOIN",
		"right": "RIGHT JOIN",
		"full":  "FULL JOIN",
	}
	for _, j := range df.joins {
		var rSQL string
		if j.right.needsSubquery() || len(j.right.filters) > 0 || len(j.right.joins) > 0 {
			inner, err := j.right.compileInto(p)
			if err != nil {
				return "", err
			}
			rSQL = "(" + inner + ")"
		} else {
			rSQL = rebindParams(j.right.sourceSQL, j.right.sourceParams, p)
		}
		rAlias, err := quoteIdent(j.rightAlias)
		if err != nil {
			return "", err
		}
		var onSQL string
		if j.onExpr != nil {
			onSQL, err = j.onExpr.compile(p)
			if err != nil {
				return "", err
			}
		} else {
			lAlias, err := quoteIdent(df.sourceAlias)
			if err != nil {
				return "", err
			}
			lc, err := quoteIdent(j.leftCol)
			if err != nil {
				return "", err
			}
			rc, err := quoteIdent(j.rightCol)
			if err != nil {
				return "", err
			}
			onSQL = fmt.Sprintf("%s.%s = %s.%s", lAlias, lc, rAlias, rc)
		}
		parts = append(parts, fmt.Sprintf("%s %s AS %s ON %s", joinKW[j.how], rSQL, rAlias, onSQL))
	}

	if len(df.filters) > 0 {
		conds := make([]string, len(df.filters))
		for i, f := range df.filters {
			s, err := f.compile(p)
			if err != nil {
				return "", err
			}
			conds[i] = s
		}
		parts = append(parts, "WHERE "+strings.Join(conds, " AND "))
	}
	if len(df.groupBy) > 0 {
		if len(df.selects) >= len(df.groupBy) {
			pos := make([]string, len(df.groupBy))
			for i := range df.groupBy {
				pos[i] = strconv.Itoa(i + 1)
			}
			parts = append(parts, "GROUP BY "+strings.Join(pos, ", "))
		} else {
			gbs := make([]string, len(df.groupBy))
			for i, g := range df.groupBy {
				s, err := g.compile(p)
				if err != nil {
					return "", err
				}
				gbs[i] = s
			}
			parts = append(parts, "GROUP BY "+strings.Join(gbs, ", "))
		}
	}
	if len(df.having) > 0 {
		hvs := make([]string, len(df.having))
		for i, h := range df.having {
			s, err := h.compile(p)
			if err != nil {
				return "", err
			}
			hvs[i] = s
		}
		parts = append(parts, "HAVING "+strings.Join(hvs, " AND "))
	}
	if len(df.orders) > 0 {
		ords := make([]string, len(df.orders))
		for i, o := range df.orders {
			s, err := o.compile(p)
			if err != nil {
				return "", err
			}
			ords[i] = s
		}
		parts = append(parts, "ORDER BY "+strings.Join(ords, ", "))
	}
	if df.limit != nil {
		parts = append(parts, fmt.Sprintf("LIMIT %d", *df.limit))
	}
	if df.offset != nil {
		parts = append(parts, fmt.Sprintf("OFFSET %d", *df.offset))
	}
	return strings.Join(parts, " "), nil
}

// ToSQL compiles the lazy DataFrame into (statement, params).
func (df *DataFrame) ToSQL() (string, []any, error) {
	p := &paramCollector{}
	sql, err := df.compileInto(p)
	if err != nil {
		return "", nil, err
	}
	return sql, p.values, nil
}

// Collect materializes the DataFrame as untyped rows (using streaming when unbounded or >10k).
func (df *DataFrame) Collect(ctx context.Context) ([]Row, error) {
	return CollectAs[Row](ctx, df)
}

// CollectAs materializes the DataFrame into a typed slice []T.
func CollectAs[T any](ctx context.Context, df *DataFrame) ([]T, error) {
	sql, params, err := df.ToSQL()
	if err != nil {
		return nil, err
	}
	if df.limit == nil || *df.limit > 10000 {
		capVal := 100000
		if df.limit != nil {
			capVal = *df.limit
		}
		rs, err := StreamAs[T](ctx, df.session, sql, params, &StreamOptions{RowCap: capVal})
		if err != nil {
			return nil, err
		}
		return rs.All()
	}
	return QueryAs[T](ctx, df.session, sql, params...)
}

// Stream lazily streams untyped rows via POST /v1/sql/stream.
func (df *DataFrame) Stream(ctx context.Context, opts *StreamOptions) (*RowStream[Row], error) {
	return StreamDF[Row](ctx, df, opts)
}

// StreamDF lazily streams typed structs T from a DataFrame via POST /v1/sql/stream.
func StreamDF[T any](ctx context.Context, df *DataFrame, opts *StreamOptions) (*RowStream[T], error) {
	sql, params, err := df.ToSQL()
	if err != nil {
		return nil, err
	}
	return StreamAs[T](ctx, df.session, sql, params, opts)
}

// First pushes down LIMIT 1 and returns the first row or nil.
func (df *DataFrame) First(ctx context.Context) (Row, error) {
	rows, err := df.Limit(1).Collect(ctx)
	if err != nil || len(rows) == 0 {
		return nil, err
	}
	return rows[0], nil
}

// Count pushes down SELECT count(*) AS n to PostgreSQL.
func (df *DataFrame) Count(ctx context.Context) (int64, error) {
	sql, params, err := df.ToSQL()
	if err != nil {
		return 0, err
	}
	type countRow struct {
		N int64 `json:"n"`
	}
	rows, err := QueryAs[countRow](ctx, df.session, "SELECT count(*) AS n FROM ("+sql+") AS _cnt", params...)
	if err != nil || len(rows) == 0 {
		return 0, err
	}
	return rows[0].N, nil
}

// ToCSV streams RFC 4180 CSV bytes from POST /v1/sql/stream.
func (df *DataFrame) ToCSV(ctx context.Context, opts *StreamOptions) (io.ReadCloser, error) {
	sql, params, err := df.ToSQL()
	if err != nil {
		return nil, err
	}
	return df.session.StreamCSV(ctx, sql, params, opts)
}
