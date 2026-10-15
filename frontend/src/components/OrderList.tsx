import type { Order } from "../api/types";
import { formatCents, formatDate } from "../lib/format";
import { isAgentOrder } from "../lib/prompts";
import { Badge, SimulatedBadge } from "./Badge";

interface Props {
  orders: Order[] | undefined;
  loading: boolean;
  error: string | null;
  selectedOrderId: string | null;
  onSelect: (orderId: string | null) => void;
  onRetry: () => void;
  /** Demo control for agent-placed orders that have not been delivered yet. */
  onSimulateDelivery: (orderId: string) => void;
  deliveryBusyId: string | null;
  deliveryError: string | null;
}

export function OrderList({
  orders,
  loading,
  error,
  selectedOrderId,
  onSelect,
  onRetry,
  onSimulateDelivery,
  deliveryBusyId,
  deliveryError,
}: Props) {
  return (
    <section className="card" aria-labelledby="orders-title" data-testid="orders">
      <div className="card-head">
        <h3 id="orders-title">Your orders</h3>
        {selectedOrderId && (
          <button type="button" className="btn btn-link" onClick={() => onSelect(null)}>
            Clear selection
          </button>
        )}
      </div>

      {loading && <p role="status">Loading orders...</p>}
      {error && (
        <div className="alert alert-error" role="alert">
          <p>{error}</p>
          <button type="button" className="btn" onClick={onRetry}>
            Try again
          </button>
        </div>
      )}
      {deliveryError && (
        <p className="alert alert-error" role="alert">
          {deliveryError}
        </p>
      )}
      {orders && orders.length === 0 && <p>No orders found for this customer.</p>}

      {orders && orders.length > 0 && (
        <fieldset className="order-fieldset">
          <legend className="sr-only">Select the order your message is about</legend>
          <ul className="order-list">
            {orders.map((o) => {
              const checked = o.order_id === selectedOrderId;
              const agentOrder = isAgentOrder(o);
              return (
                <li key={o.order_id} className="order-wrap">
                  <label className={`order-item${checked ? " is-selected" : ""}`} data-testid={`order-${o.order_id}`}>
                    <input type="radio" name="order" value={o.order_id} checked={checked} onChange={() => onSelect(o.order_id)} />
                    <span className="order-body">
                      <span className="order-top">
                        <span className="mono order-id">{o.order_id}</span>
                        <SimulatedBadge />
                        {agentOrder && <Badge tone="neutral">Placed by ALDO bot</Badge>}
                        {o.lines.map((l) => (
                          <span className="order-tags" key={l.line_id}>
                            {l.final_sale === true && <Badge tone="neutral">Final sale</Badge>}
                            {l.returned_or_reserved_qty > 0 && (
                              <Badge tone="neutral">
                                {l.returned_or_reserved_qty} of {l.purchased_qty} already returned
                              </Badge>
                            )}
                          </span>
                        ))}
                      </span>
                      {o.lines.map((l) => (
                        <span className="order-product" key={l.line_id}>
                          {l.product_name}
                          <span className="muted">
                            {" "}
                            size {l.size}
                            {l.color ? `, ${l.color.toLowerCase()}` : ""}
                            {l.purchased_qty > 1 ? `, qty ${l.purchased_qty}` : ""}
                          </span>
                        </span>
                      ))}
                      <span className="order-dates muted">
                        {o.lines.map((l) => {
                          const total = l.unit_paid_cents * l.purchased_qty;
                          return `Paid ${formatCents(total, o.currency)}${l.purchased_qty > 1 ? ` (${l.purchased_qty} x ${formatCents(l.unit_paid_cents, o.currency)})` : ""}. `;
                        })}
                        Ordered {formatDate(o.ordered_at)}. Delivered {o.delivered_at ? formatDate(o.delivered_at) : "not yet"}.
                      </span>
                    </span>
                  </label>
                  {agentOrder && !o.delivered_at && (
                    <div className="demo-control">
                      <button
                        type="button"
                        className="btn btn-demo"
                        disabled={deliveryBusyId === o.order_id}
                        onClick={() => onSimulateDelivery(o.order_id)}
                        data-testid={`simulate-delivery-${o.order_id}`}
                      >
                        {deliveryBusyId === o.order_id ? "Simulating..." : "Simulate delivery"}
                      </button>
                      <span className="muted">Demo control: marks this order delivered today so a return can start.</span>
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        </fieldset>
      )}
    </section>
  );
}
