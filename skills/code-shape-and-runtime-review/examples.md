# Style by example

Invented examples from an inventory service. Each one shows a style the rules in [SKILL.md](SKILL.md) describe.

## A closed variant type

A shipment goes either to a warehouse or to a pickup point. Each wire form carries only the ID its kind selects, so the unselected ID is absent structurally. Decoding reads the kind first and rejects a missing or unknown kind instead of guessing.

```go
type DestinationKind string

const (
	DestinationWarehouse   DestinationKind = "warehouse"
	DestinationPickupPoint DestinationKind = "pickup_point"
)

type Destination struct {
	kind          DestinationKind
	warehouseID   ids.Warehouse
	pickupPointID ids.PickupPoint
}

func (d *Destination) UnmarshalJSON(data []byte) error {
	var head struct {
		Kind DestinationKind `json:"kind"`
	}
	if err := json.Unmarshal(data, &head); err != nil {
		return err
	}

	switch head.Kind {
	case DestinationWarehouse:
		var w struct {
			WarehouseID ids.Warehouse `json:"warehouse_id"`
		}
		if err := json.Unmarshal(data, &w); err != nil {
			return err
		}
		*d = Destination{kind: head.Kind, warehouseID: w.WarehouseID}
		return nil
	case DestinationPickupPoint:
		var p struct {
			PickupPointID ids.PickupPoint `json:"pickup_point_id"`
		}
		if err := json.Unmarshal(data, &p); err != nil {
			return err
		}
		*d = Destination{kind: head.Kind, pickupPointID: p.PickupPointID}
		return nil
	case "":
		return errors.New("destination kind is missing")
	}

	return fmt.Errorf("unknown destination kind %q", head.Kind)
}
```

## Bookkeeping that must outlive the caller

A one-line reason above the line that needs it, a logger scoped with IDs, and a secondary failure joined to the primary one. `errors.Join` drops nil arguments, so the same call covers a recount that succeeded and one that failed.

```go
func (s *Service) finishRecount(ctx context.Context, job RecountJob, runErr error) error {
	logger := s.logger.With(
		zap.Stringer("warehouse_id", job.WarehouseID),
		zap.Stringer("job_id", job.ID),
	)

	status := RecountSucceeded
	if runErr != nil {
		status = RecountFailed
		logger.Warn("Stock recount failed", zap.Error(runErr))
	}

	// A canceled caller must not leave the job marked as running.
	recordCtx := context.WithoutCancel(ctx)
	if err := s.store.SetRecountStatus(recordCtx, job.ID, status); err != nil {
		return errors.Join(runErr, fmt.Errorf("record recount status: %w", err))
	}
	return runErr
}
```

## A deliberate duplicate, a contract docstring, and pinned settings

The drift warning sits at the declaration. The docstring states what the caller gets, including the query ID an operator searches for. Each pinned database setting carries the reason it matters.

```go
// The low-stock filter in this statement is repeated in reorder_report.sql.
// Change both files together. Otherwise, the reorder report lists items that
// the low-stock page does not show.
//
//go:embed low_stock.sql
var lowStockStatement string

// ExportLowStock writes one warehouse's low-stock items to w. The database
// query ID is the export ID, so an operator can find the query in the query
// log. After an error, w may hold a partial export.
func (s *Store) ExportLowStock(ctx context.Context, in ExportLowStockInput, w io.Writer) error {
	ctx = analyticsdb.WithQueryID(ctx, in.ExportID.String())
	ctx = analyticsdb.WithSettings(ctx, analyticsdb.Settings{
		// A result cut off at the row limit would look like a complete export.
		"result_overflow_mode": "throw",
	})
	...
}
```

## A spy that embeds the store

The spy records one method's input and delegates to the real store. One test function runs against the fake and the real stores.

```go
type reservationRecorder struct {
	inventory.Store
	reserved []inventory.ReserveInput
}

func (r *reservationRecorder) Reserve(ctx context.Context, in inventory.ReserveInput) error {
	r.reserved = append(r.reserved, in)
	return r.Store.Reserve(ctx, in)
}

func TestOrderPlacement(t *testing.T) { runAgainstEachStore(t, testOrderPlacement) }
```
