// Tests for the object pool and intrusive queue on their own, below the book.
#include <gtest/gtest.h>

#include <vector>

#include "lob/order_queue.hpp"

namespace {

using lob::IntrusiveQueue;
using lob::Order;
using lob::OrderNode;
using lob::OrderPool;
using lob::Side;

Order make_order(lob::OrderId id) {
    return Order{.id = id, .price = 100, .qty = 1, .side = Side::Buy};
}

TEST(OrderPoolTest, ReleasedNodeIsReusedFirst) {
    OrderPool pool(8);
    OrderNode* a = pool.allocate();
    pool.release(a);
    EXPECT_EQ(pool.allocate(), a);   // LIFO free list: same address comes straight back
}

TEST(OrderPoolTest, GrowsByWholeChunksAndTracksLiveCount) {
    OrderPool pool(4);
    EXPECT_EQ(pool.capacity(), 0u);   // nothing allocated until first use

    std::vector<OrderNode*> nodes;
    for (int i = 0; i < 5; ++i) {
        nodes.push_back(pool.allocate());
    }
    EXPECT_EQ(pool.capacity(), 8u);   // the fifth node forced a second chunk
    EXPECT_EQ(pool.live(), 5u);

    for (OrderNode* node : nodes) {
        pool.release(node);
    }
    EXPECT_EQ(pool.live(), 0u);
    EXPECT_EQ(pool.capacity(), 8u);   // memory is kept for reuse, not returned
}

TEST(OrderPoolTest, PointersStayValidWhenThePoolGrows) {
    OrderPool pool(2);
    OrderNode* first = pool.allocate();
    first->order = make_order(7);

    std::vector<OrderNode*> more;
    for (int i = 0; i < 100; ++i) {   // forces ~50 more chunks
        more.push_back(pool.allocate());
    }
    // If growth moved nodes, this read would be a use-after-free, which the
    // AddressSanitizer build would report.
    EXPECT_EQ(first->order.id, 7u);
}

TEST(OrderPoolTest, SteadyStateChurnDoesNotGrowThePool) {
    OrderPool pool(16);
    IntrusiveQueue queue;
    for (lob::OrderId id = 1; id <= 10; ++id) {
        queue.push_back(pool, make_order(id));
    }
    const std::size_t capacity = pool.capacity();

    for (lob::OrderId id = 11; id <= 10'000; ++id) {   // one out, one in
        queue.pop_front(pool);
        queue.push_back(pool, make_order(id));
    }
    EXPECT_EQ(pool.capacity(), capacity);
    EXPECT_EQ(pool.live(), 10u);
    EXPECT_EQ(queue.front().id, 9'991u);
}

TEST(IntrusiveQueueTest, EraseHeadMiddleAndTail) {
    OrderPool pool(8);
    IntrusiveQueue queue;
    OrderNode* a = queue.push_back(pool, make_order(1));
    OrderNode* b = queue.push_back(pool, make_order(2));
    OrderNode* c = queue.push_back(pool, make_order(3));
    OrderNode* d = queue.push_back(pool, make_order(4));

    queue.erase(pool, b);   // middle
    EXPECT_EQ(queue.size(), 3u);
    EXPECT_EQ(queue.front().id, 1u);

    queue.erase(pool, a);   // head
    EXPECT_EQ(queue.front().id, 3u);

    queue.erase(pool, d);   // tail
    EXPECT_EQ(queue.size(), 1u);
    EXPECT_EQ(queue.front().id, 3u);

    // After removing the tail, a new order must link in behind `c`.
    queue.push_back(pool, make_order(5));
    queue.erase(pool, c);
    EXPECT_EQ(queue.front().id, 5u);

    queue.pop_front(pool);
    EXPECT_TRUE(queue.empty());
    EXPECT_EQ(pool.live(), 0u);
}

}  // namespace
